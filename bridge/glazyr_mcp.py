#!/usr/bin/env python3
"""Glazyr MCP bridge v0.3.2.

v0.3.2: resident mode (--resident) - one bridge per machine serves
every client over a Unix socket under ~/.glazyr, so per-call clients
(mcporter and friends) keep page state between invocations; the
launcher attaches stdio to the socket. Plus producer liveness: reads
verify the browser producer is alive before serving, and a dead
producer yields an explicit GLAZYR_PRODUCER_DEAD error and one
respawn - stale state is never served as live (the Nov 21 wedge).

(Original v0.2.3 notes follow.)

An MCP server (stdio) that gives an agent the Glazyr browser:
- reads page state from the semantic shared-memory region the patched
  headless_shell producer writes (microsecond reads, filtered by
  glazyr_filter),
- acts via CDP Input events at the geometry the proto carries for each
  node (v0 action path; the in-renderer PerformAction channel is the
  follow-on),
- meters tokens served.

v0.2 (from two independent dogfood reports, Nov 21): unchanged-state
stubs, compact acks on non-navigating clicks, a bridge-asserted action
overlay (typed values / CHECKED), URL+title headers, level control on
navigate/click/scroll, glazyr_find + glazyr_peek_subtree, read latency
in the footer, a desktop user-agent, and stale node-id re-resolution.

v0.2.3 (from the Nov 21 four-client dogfood): the CDP command channel
binds to the page target the state channel is writing for - never
"first page in the list" - so a second tab can no longer split the
channels and stall every navigate behind a frozen page; a navigate
that cannot be confirmed against the state channel retries once on a
relaunched browser and then fails honestly instead of serving the
previous page; status reports page-target count, channel agreement,
and state age; find matches heading text and the page title.

Runs inside WSL on the workstation, next to the browser. The client
(Claude Code / Codex / any MCP client) spawns it over stdio.
"""
import fcntl, json, os, struct, subprocess, sys, threading, time, urllib.request
from urllib.parse import urlparse

# FastMCP's settings read .env from the CWD; pin CWD to the bridge dir so
# any client's working directory can't break startup.
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import glazyr_filter as gf
from glazyr_filter import cq, T

from mcp.server.fastmcp import FastMCP

SHELL = os.environ.get("GLAZYR_SHELL", "/root/chromium/src/out/Default/headless_shell")
SEM_SIZE = 4 * 1024 * 1024
HDR = 144
CDP_PORT = 9224
# Desktop UA for the same Chromium version: headless_shell's default
# HeadlessChrome UA is a bot-detection signal (both dogfood agents were
# throttled by Hacker News). Whether this lifts the throttle is a test,
# recorded in the dogfood notes either way.
USER_AGENT = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36')

mcp = FastMCP("glazyr")

_state = {
    'browser': None,      # subprocess.Popen
    'ws': None,           # CDP websocket
    'bound_target': None, # CDP target id the ws is bound to (v0.2.3)
    'cdp_id': 0,
    'seq': 0,
    'seq_at': 0,          # wall time when seq last advanced (v0.2.3)
    'apc': None,          # last parsed AnnotatedPageContent
    'nodes': {},          # dom_id -> ContentNode
    'sigs': {},           # dom_id -> identity signature (current page)
    'prev_sigs': {},      # dom_id -> signature (previous page, for re-resolution)
    'overlay': {},        # dom_id -> {kind: annotation} bridge-asserted actions
    'overlay_version': 0,
    'last_served': None,  # (seq, level, overlay_version) of last full peek
    'doc_key': None,      # main_frame_data.document_identifier of current state
    'page_url': '',
    'page_title': '',
    'served_history': [], # [{url, seq, sigs}] recent served states (stale-id intent)
    'last_error': None,   # last command-channel failure, for glazyr_status
    'tokens_served': 0,
    'reads': 0,
    'producer_died': None,  # v0.3.2: {url, seq, detail} of the last producer death
    'probe_cache': (0.0, None),  # v0.3.2: (checked_at, health dict)
    'resident': None,     # v0.3.2: resident-mode bookkeeping, when resident
}

# ---------- tokenizer (yardstick; falls back to chars/4) ----------
try:
    import tiktoken
    _enc = tiktoken.get_encoding('cl100k_base')
    def count_tokens(s):
        return len(_enc.encode(s))
except Exception:
    def count_tokens(s):
        return max(1, len(s) // 4)

# ---------- shared memory ----------
def find_sem():
    """Locate the producer's shared-memory region. Returns
    (fd_path, producer_pid) or (None, None)."""
    for pid in [p for p in os.listdir('/proc') if p.isdigit()]:
        try:
            cmd = open(f'/proc/{pid}/cmdline', 'rb').read().decode(errors='ignore')
        except Exception:
            continue
        if 'headless_shell' not in cmd:
            continue
        fd_dir = f'/proc/{pid}/fd'
        try:
            fds = os.listdir(fd_dir)
        except Exception:
            continue
        for fd in fds:
            path = os.path.join(fd_dir, fd)
            try:
                if 'memfd' in os.readlink(path) and os.stat(path).st_size == SEM_SIZE:
                    return path, int(pid)
            except Exception:
                pass
    return None, None

def find_sem_fd():
    return find_sem()[0]

def read_payload():
    """Returns (seq, payload_bytes) or (0, None)."""
    path = find_sem_fd()
    if not path:
        return 0, None
    f = os.open(path, os.O_RDONLY)
    try:
        hdr = os.pread(f, HDR, 0)
        magic, _ver, seq, dsize = struct.unpack_from('<IIQI', hdr, 0)
        if magic != 0x53454D41 or seq < 1 or dsize == 0:
            return 0, None
        return seq, os.pread(f, dsize, HDR)
    finally:
        os.close(f)

def _sig_of(node):
    """Identity signature for stale-id re-resolution: what a node IS,
    independent of its per-document DOM id."""
    a = node.content_attributes
    t = T.get(a.attribute_type)
    sig = {'type': t}
    if t == 'ANCHOR':
        sig['href'] = a.anchor_data.url
    elif t == 'FORM_CONTROL':
        fc = a.form_control_data
        sig['name'] = fc.field_name
        sig['fctype'] = cq.FormControlType.Name(fc.form_control_type)
    txt = gf.node_text(node).strip()
    if txt:
        sig['text'] = txt[:60]
    return sig

def _sig_strong(sig):
    if sig.get('href'):
        return True
    if sig.get('name'):
        return True
    return len(sig.get('text', '')) >= 8

def _unwrap(v):
    return v.value if hasattr(v, 'value') else v

def refresh_apc():
    seq, payload = read_payload()
    if payload is None:
        return False
    if seq != _state['seq'] or _state['apc'] is None:
        apc = cq.AnnotatedPageContent()
        apc.ParseFromString(payload)
        nodes = {}
        sigs = {}
        def rec(n):
            did = n.content_attributes.common_ancestor_dom_node_id
            if did:
                nodes[did] = n
                sigs[did] = _sig_of(n)
            for c in n.children_nodes:
                rec(c)
        rec(apc.root_node)
        fd = apc.main_frame_data
        doc_key = _unwrap(fd.document_identifier) or None
        # A producer rewrite of the SAME document (same identifier) is
        # not a navigation: ids stay valid and bridge-asserted overlay
        # annotations still apply. Only a new document rotates history.
        same_doc = doc_key is not None and doc_key == _state['doc_key']
        if _state['apc'] is not None and seq != _state['seq'] and not same_doc:
            _state['prev_sigs'] = _state['sigs']
            _state['overlay'] = {}
            _state['overlay_version'] += 1
        _state['seq'] = seq
        _state['seq_at'] = time.time()
        _state['apc'] = apc
        _state['nodes'] = nodes
        _state['sigs'] = sigs
        _state['doc_key'] = doc_key
        _state['page_url'] = _unwrap(fd.url) or ''
        _state['page_title'] = _unwrap(fd.title) or ''
    return True

def wait_for_new_seq(old_seq, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        seq, _ = read_payload()
        if seq > old_seq:
            time.sleep(0.15)
            refresh_apc()
            return True
        if 0 < seq < old_seq:
            # Sequence went backwards: the shell restarted and its
            # producer counter reset. That is a state change too.
            time.sleep(0.5)
            refresh_apc()
            return True
        time.sleep(0.1)
    refresh_apc()
    return False

# ---------- producer liveness (v0.3.2) ----------
def _pid_alive(pid):
    """True if pid exists and is not a zombie."""
    if not pid:
        return False
    try:
        with open(f'/proc/{pid}/stat', 'rb') as f:
            return f.read().rsplit(b')', 1)[1].split()[0] != b'Z'
    except Exception:
        return False

def _shell_pids():
    """PIDs of live headless_shell processes (zombies excluded)."""
    out = []
    for pid in [p for p in os.listdir('/proc') if p.isdigit()]:
        try:
            cmd = open(f'/proc/{pid}/cmdline', 'rb').read().decode(errors='ignore')
        except Exception:
            continue
        if 'headless_shell' in cmd and _pid_alive(int(pid)):
            out.append(int(pid))
    return out

def _renderer_probe(timeout=3):
    """Ask the bound page's renderer to evaluate a trivial expression
    over a throwaway CDP websocket. A live-but-idle renderer answers
    in milliseconds; a dead renderer (the Nov 21 wedge: process alive,
    seq frozen, state served as live forever) times out or errors.
    Returns True (answered) / False (failed)."""
    try:
        import websocket
        # Single-shot target fetch (not _pick_page_target, whose retry
        # loop is built for command setup, not a 3s probe).
        targets = _cdp_targets() or []
        pages = [t for t in targets if t.get('type') == 'page']
        if not pages:
            return False
        page = next((t for t in pages
                     if t.get('url') == _state['page_url']), pages[0])
        ws = websocket.create_connection(page['webSocketDebuggerUrl'],
                                         timeout=timeout)
        try:
            ws.send(json.dumps({'id': 990001, 'method': 'Runtime.evaluate',
                                'params': {'expression': '1',
                                           'returnByValue': True}}))
            deadline = time.time() + timeout
            while time.time() < deadline:
                msg = json.loads(ws.recv())
                if msg.get('id') == 990001:
                    return 'result' in msg
            return False
        finally:
            ws.close()
    except Exception:
        return False

def producer_health(force=False):
    """Is the state producer (headless_shell) alive and answering?

    Returns {'status': 'live'|'dead'|'none', 'detail': str}.
    'none' = no producer has ever run in this bridge (nothing to
    protect). Positive results are cached briefly so reads stay fast;
    the renderer probe only runs when the state has been frozen long
    enough to suspect a wedged renderer."""
    now = time.time()
    checked_at, cached = _state['probe_cache']
    if not force and cached is not None and now - checked_at < 10:
        return cached
    health = _producer_health_uncached(now)
    _state['probe_cache'] = (now, health)
    return health

def _producer_health_uncached(now):
    popen = _state['browser']
    if popen is not None and popen.poll() is not None:
        return {'status': 'dead',
                'detail': f'browser process (pid {popen.pid}) exited '
                          f'with code {popen.returncode}'}
    _path, sem_pid = find_sem()
    if sem_pid is None:
        if _state['seq'] > 0 or _state['apc'] is not None:
            return {'status': 'dead',
                    'detail': 'state region (memfd) is gone - the '
                              'producer process exited'}
        return {'status': 'none', 'detail': 'no producer yet'}
    if not _pid_alive(sem_pid):
        return {'status': 'dead',
                'detail': f'producer process (pid {sem_pid}) is gone'}
    # Producer process lives. An old seq alone is just an idle page
    # (checkpoint 7 honesty); only probe the renderer when the state
    # has been frozen long enough to suspect a wedge.
    seq_age = now - _state['seq_at'] if _state['seq_at'] else 0
    if _state['seq'] > 0 and seq_age > 45:
        if not _renderer_probe():
            return {'status': 'dead',
                    'detail': f'producer (pid {sem_pid}) is alive but '
                              f'its renderer does not answer CDP '
                              f'(seq frozen at {_state["seq"]} for '
                              f'{int(seq_age)}s)'}
    return {'status': 'live', 'detail': f'producer pid {sem_pid}'}

def _guard_state_fresh():
    """Read-path guard (v0.3.2): never serve a dead producer's state
    as live. Returns None when the state in hand may be served, or an
    explicit error string to return to the caller instead. On death:
    record it, drop the corpse's cached state, attempt ONE respawn -
    the caller still gets the error for this read, and the next read
    sees the fresh browser's real (empty) state. Failing loudly is
    the feature."""
    if _state['apc'] is None and _state['seq'] == 0:
        return None
    health = producer_health()
    if health['status'] != 'dead':
        return None
    died_url, died_seq = _state['page_url'], _state['seq']
    detail = health['detail']
    _state['producer_died'] = {'url': died_url, 'seq': died_seq,
                               'detail': detail}
    _state['last_error'] = f'GLAZYR_PRODUCER_DEAD: {detail}'
    # Drop the corpse's state so nothing downstream can serve it.
    _state['apc'] = None
    _state['nodes'] = {}
    _state['sigs'] = {}
    _state['seq'] = 0
    _state['doc_key'] = None
    _state['page_url'] = ''
    _state['page_title'] = ''
    _state['last_served'] = None
    _state['probe_cache'] = (0.0, None)
    try:
        _force_relaunch()
    except Exception as e:
        _state['last_error'] += f'; respawn failed: {e}'
    return (f'Glazyr error GLAZYR_PRODUCER_DEAD: the browser died '
            f'({detail}). Its last state (url: {died_url or "(none)"}, '
            f'seq {died_seq}) was NOT served - it is gone, not stale '
            f'content presented as live. A fresh browser has been '
            f'launched; call glazyr_navigate to continue.')

# ---------- browser + CDP ----------
def _kill_shells():
    # v0.3.2: kill by exact PID from a /proc scan (no pkill -f pattern
    # matching - the pattern can hit unrelated processes, and house
    # discipline is PID-only).
    for pid in _shell_pids():
        try:
            os.kill(pid, 9)
        except Exception:
            pass
    for _ in range(20):
        if not _shell_pids():
            break
        time.sleep(0.3)
    _state['ws'] = None
    _state['bound_target'] = None

def _start_browser():
    _state['browser'] = subprocess.Popen(
        [SHELL, '--no-sandbox', '--glazyr-producer', '--window-size=1280,1024',
         f'--user-agent={USER_AGENT}',
         f'--remote-debugging-port={CDP_PORT}', 'about:blank'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)  # own process group: clean group kill, no orphans
    for _ in range(60):
        if _cdp_targets():
            break
        time.sleep(0.3)
    return True

def ensure_browser():
    # Healthy = CDP answers. Anything else (no shell, stale shell from a
    # dead session, port held by a zombie) gets replaced with a fresh one.
    if _cdp_targets():
        return True
    _kill_shells()
    return _start_browser()

def _force_relaunch():
    """v0.2.3 recovery: the browser answers CDP but its page never
    advances state (dead renderer, wedged target - test 1b). Kill it
    unconditionally and start fresh; ensure_browser's health check
    (CDP answers) cannot see this failure."""
    _kill_shells()
    return _start_browser()

def _cdp_targets():
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{CDP_PORT}/json', timeout=2) as r:
            return json.loads(r.read())
    except Exception:
        return None

def _pick_page_target():
    """Choose the CDP page target the command channel binds to.

    v0.2.3: with more than one page target open (a popup, a
    target=_blank link, a restored tab), "first page in the list"
    can be a different page than the one the producer writes state
    for - commands then navigate one tab while reads serve another,
    and every navigate stalls behind a frozen seq. The state channel
    is the ground truth: bind to the target whose URL is the state's
    URL, then to a host match, and only then to the first page -
    recording the ambiguity in last_error when we had to guess."""
    targets = None
    for _ in range(40):
        targets = _cdp_targets()
        if targets:
            break
        time.sleep(0.3)
    pages = [t for t in (targets or []) if t.get('type') == 'page']
    if not pages:
        return None
    if len(pages) == 1:
        return pages[0]
    refresh_apc()
    state_url = _state['page_url']
    if state_url:
        for t in pages:
            if t.get('url') == state_url:
                return t
        state_host = urlparse(state_url).hostname
        for t in pages:
            if urlparse(t.get('url', '')).hostname == state_host:
                _state['last_error'] = (
                    f'multiple page targets ({len(pages)}); bound the '
                    f'command channel by host match to {t.get("url")}')
                return t
    _state['last_error'] = (
        f'multiple page targets ({len(pages)}) and none matches the '
        f'state channel; bound to the first page target')
    return pages[0]

def cdp_ws():
    if _state['ws'] is not None:
        return _state['ws']
    import websocket
    page = _pick_page_target()
    _state['bound_target'] = page['id']
    _state['ws'] = websocket.create_connection(page['webSocketDebuggerUrl'], timeout=30)
    return _state['ws']

def cdp_send(method, params=None):
    """One CDP round trip returning the full message dict containing
    either 'result' or 'error'."""
    for attempt in (1, 2):
        try:
            ws = cdp_ws()
            _state['cdp_id'] += 1
            mid = _state['cdp_id']
            ws.send(json.dumps({'id': mid, 'method': method, 'params': params or {}}))
            while True:
                msg = json.loads(ws.recv())
                if msg.get('id') == mid:
                    return msg
        except Exception as e:
            _state['ws'] = None
            _state['bound_target'] = None
            _state['last_error'] = f'{method}: {type(e).__name__}: {e}'
            if attempt == 2:
                raise
            ensure_browser()
    return {}

def cdp(method, params=None):
    """One CDP round trip. Returns 'result' dict (default empty dict)."""
    return cdp_send(method, params).get('result', {})

# ---------- peek ----------
def page_info():
    """(url, title) of the current page - carried in the proto itself
    (main_frame_data), with a CDP fallback. Agents should never have to
    infer where they are from an href."""
    if _state['page_url']:
        return _state['page_url'], _state['page_title']
    try:
        r = cdp('Runtime.evaluate', {
            'expression': 'JSON.stringify({u: location.href, t: document.title})',
            'returnByValue': True})
        v = json.loads(r['result']['value'])
        return v.get('u', ''), v.get('t', '')
    except Exception:
        return '', ''

def _header():
    url, title = page_info()
    if url:
        return f'# url: {url}' + (f' | title: {title}' if title else '')
    return ''

def _footer(toks, read_ms, level):
    return (f'# glazyr seq={_state["seq"]} level={level} read_ms={read_ms} '
            f'tokens_this_read={toks} tokens_served_total={_state["tokens_served"]}')

def do_peek(level='standard', force=False):
    ensure_browser()
    if not refresh_apc():
        return 'Glazyr: no page state yet. Navigate somewhere first.'
    err = _guard_state_fresh()
    if err:
        return err
    key = (_state['seq'], level, _state['overlay_version'])
    if not force and level != 'none' and _state['last_served'] == key:
        stub = (_header() +
                f'\n# unchanged seq={_state["seq"]} level={level}: state is '
                f'identical to the previous read. Pass force=true to resend.')
        toks = count_tokens(stub)
        _state['tokens_served'] += toks
        return stub + f'\n# glazyr tokens_this_read={toks} tokens_served_total={_state["tokens_served"]}'
    t0 = time.perf_counter()
    overlay = _state['overlay'] if level != 'none' else None
    text, _st = gf.serialize(_state['apc'], level, overlay)
    read_ms = round((time.perf_counter() - t0) * 1000)
    hdr = _header()
    if hdr:
        text = hdr + '\n' + text
    toks = count_tokens(text)
    _state['tokens_served'] += toks
    _state['reads'] += 1
    if level != 'none':
        _state['last_served'] = key
        _mark_served()
    return text + '\n' + _footer(toks, read_ms, level)

def page_summary():
    """v0.2.2 summary-first navigate: the map, not the territory. URL,
    title, size counts, and the heading outline - enough to understand
    the page's shape and choose a zoom (find / subtree / peek) instead
    of paying for the full state up front."""
    t0 = time.perf_counter()
    apc = _state['apc']
    f = gf.Filter(apc, 'standard')
    counts = {'total': 0, 'links': 0, 'inputs': 0}
    headings = []
    def rec(n):
        counts['total'] += 1
        t = T.get(n.content_attributes.attribute_type)
        if t == 'ANCHOR':
            counts['links'] += 1
        elif t == 'FORM_CONTROL':
            counts['inputs'] += 1
        if t == 'HEADING':
            txt = ' '.join(f.merged_text(n).split())[:90]
            if txt:
                headings.append(txt)
            return
        for c in n.children_nodes:
            rec(c)
    rec(apc.root_node)
    read_ms = round((time.perf_counter() - t0) * 1000)
    lines = [_header(),
             f'# total={counts["total"]} nodes · links={counts["links"]} '
             f'· inputs={counts["inputs"]} · headings={len(headings)}',
             '# outline:']
    for h in headings[:80]:
        lines.append(f'- heading {h!r}')
    if len(headings) > 80:
        lines.append(f'- … +{len(headings) - 80} more headings')
    lines.append('# summary only - glazyr_peek for full state, '
                 'glazyr_find to locate, glazyr_peek_subtree to zoom. '
                 'Navigate with full=true to get the state directly.')
    text = '\n'.join(l for l in lines if l)
    toks = count_tokens(text)
    _state['tokens_served'] += toks
    _state['reads'] += 1
    _mark_served()
    return text + '\n' + _footer(toks, read_ms, 'summary')

def _rematch(sig, stale_id):
    """Find the current node whose identity signature equals a stale
    id's remembered signature. Unique match or nothing."""
    matches = [did for did, s in _state['sigs'].items() if s == sig]
    if len(matches) != 1:
        return None, None
    new_id = matches[0]
    key = 'href' if sig.get('href') else ('name' if sig.get('name') else 'text')
    return _state['nodes'][new_id], (
        f'# note: node {stale_id} was from a previous load of this page; '
        f're-matched to [{new_id}] by {key}.')

def resolve_node(node_id):
    """Find a node by id, guarding against stale ids. DOM ids are
    per-document integers: after a reload of the same page they shift,
    and a stale id can be absent (re-match by identity signature from
    the last served load of this URL) or collide with a different
    element (refuse, never click blind). Returns (node, note), or
    (None, warning)."""
    refresh_apc()
    node = _state['nodes'].get(node_id)
    cur_sig = _state['sigs'].get(node_id)
    hist_sig = None
    for e in reversed(_state['served_history']):
        if e['seq'] != _state['seq'] and e['url'] == _state['page_url'] \
                and node_id in e['sigs']:
            hist_sig = e['sigs'][node_id]
            break
    if node is not None:
        if hist_sig is not None and hist_sig != cur_sig:
            m_node, m_note = _rematch(hist_sig, node_id)
            if m_node is not None:
                return m_node, m_note
            return None, (f'Glazyr: node {node_id} meant '
                          f'{hist_sig.get("type")} '
                          f'{hist_sig.get("href") or hist_sig.get("name") or hist_sig.get("text", "")!r} '
                          f'on the previous load of this page, and that '
                          f'element cannot be uniquely identified now. '
                          f'Peek again and use a fresh id.')
        return node, None
    sig = hist_sig or _state['prev_sigs'].get(node_id)
    if sig and _sig_strong(sig):
        m_node, m_note = _rematch(sig, node_id)
        if m_node is not None:
            return m_node, m_note
    return None, None

def _node_desc(node_id, node):
    sig = _state['sigs'].get(node_id, {})
    t = sig.get('type', '?')
    detail = sig.get('name') or sig.get('href') or sig.get('text', '')
    return f'[{node_id}] {t} {detail!r}' if detail else f'[{node_id}] {t}'

def _overlay_set(node_id, kind, annotation):
    _state['overlay'].setdefault(node_id, {})[kind] = annotation
    _state['overlay_version'] += 1

def _mark_served():
    """Record the current state as one the agent has seen - the intent
    record stale-id resolution reasons from."""
    entry = {'url': _state['page_url'], 'seq': _state['seq'],
             'sigs': _state['sigs']}
    h = _state['served_history']
    h[:] = [e for e in h if e['seq'] != entry['seq']]
    h.append(entry)
    del h[:-6]

# ---------- tools ----------
def _click_node(node):
    """Click a node's center. If its visible box is empty (below the fold),
    scroll by the offset implied by its outer box first, then click at the
    adjusted point. Returns None on success, or an error string."""
    g = node.content_attributes.geometry
    vb = g.visible_bounding_box if g.HasField('visible_bounding_box') else None
    ob = g.outer_bounding_box if g.HasField('outer_bounding_box') else None
    vh = 1024
    try:
        vh = _state['apc'].viewport_geometry.height or 1024
    except Exception:
        pass
    if vb is not None and vb.width > 0 and vb.height > 0:
        x, y = vb.x + vb.width / 2, vb.y + vb.height / 2
    elif ob is not None and ob.width > 0 and ob.height > 0 and ob.x >= 0 and (ob.y >= vh or ob.y + ob.height <= 0):
        dy = (ob.y + ob.height / 2) - vh / 2
        cdp('Input.dispatchMouseEvent', {'type': 'mouseWheel', 'x': 400, 'y': 300,
                                         'deltaX': 0, 'deltaY': dy})
        time.sleep(0.5)
        x, y = ob.x + ob.width / 2, ob.y + ob.height / 2 - dy
    else:
        return 'node has no geometry to click'
    cdp('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': x, 'y': y})
    cdp('Input.dispatchMouseEvent', {'type': 'mousePressed', 'x': x, 'y': y, 'button': 'left', 'clickCount': 1})
    cdp('Input.dispatchMouseEvent', {'type': 'mouseReleased', 'x': x, 'y': y, 'button': 'left', 'clickCount': 1})
    return None

def _click_with_fallback(node, node_id):
    """Click a node using layout geometry (normal mouse event path).
    When geometry lookup fails (or returns an empty/zero box), fall back in order:
      1. Scroll into view + retry geometry once (scrollIntoViewIfNeeded / JS scrollIntoView).
      2. DOM click: resolve node to its DOM element and invoke HTMLElement.click().
      3. Form submit: if node is a submit control inside a form and step 2 somehow no-ops,
         call form.requestSubmit() on the owning form.
    Returns None on success, or an informative error string naming what was tried."""
    normal_err = _click_node(node)
    if normal_err is None:
        return None

    # Geometry lookup failed or zero box. Enter fallback chain.
    real_id = node.content_attributes.common_ancestor_dom_node_id
    if not real_id:
        return 'node has no geometry to click and no DOM id to resolve'

    # Resolve node to its DOM element via CDP
    cdp('DOM.enable')
    cdp('DOM.getDocument', {'depth': 1})
    res = cdp_send('DOM.resolveNode', {'backendNodeId': real_id})
    if 'error' in res or not res.get('result', {}).get('object', {}).get('objectId'):
        err_msg = res.get('error', {}).get('message', 'not found')
        return (f'target node {node_id} (DOM {real_id}) no longer resolves in the page '
                f'at click time ({err_msg}); peek again and use a fresh id')

    oid = res['result']['object']['objectId']

    # Step 1: Scroll into view + retry geometry once
    step1_js = """function() {
        let scrollErr = null;
        try {
            if (typeof this.scrollIntoViewIfNeeded === 'function') {
                this.scrollIntoViewIfNeeded();
            } else if (typeof this.scrollIntoView === 'function') {
                this.scrollIntoView({block: 'center', inline: 'center'});
            }
        } catch (e) {
            scrollErr = (e && e.message) ? e.message : String(e);
        }
        let rect = null;
        try {
            if (typeof this.getBoundingClientRect === 'function') {
                const r = this.getBoundingClientRect();
                rect = {x: r.x, y: r.y, left: r.left, top: r.top, width: r.width, height: r.height};
            }
        } catch (e) {
            if (!scrollErr) scrollErr = (e && e.message) ? e.message : String(e);
        }
        return {scrollErr: scrollErr, rect: rect};
    }"""
    call1 = cdp_send('Runtime.callFunctionOn', {
        'objectId': oid,
        'functionDeclaration': step1_js,
        'returnByValue': True
    })
    s1_data = call1.get('result', {}).get('result', {}).get('value', {})
    rect = s1_data.get('rect') if isinstance(s1_data, dict) else None
    scroll_err = s1_data.get('scrollErr') if isinstance(s1_data, dict) else None

    vw = 1280
    vh = 1024
    try:
        if _state['apc'] and _state['apc'].viewport_geometry:
            vw = _state['apc'].viewport_geometry.width or 1280
            vh = _state['apc'].viewport_geometry.height or 1024
    except Exception:
        pass

    if rect and rect.get('width', 0) > 0 and rect.get('height', 0) > 0:
        x = rect.get('left', 0) + rect.get('width', 0) / 2
        y = rect.get('top', 0) + rect.get('height', 0) / 2
        if 0 <= x <= vw and 0 <= y <= vh:
            # Layout geometry recovered in viewport after scroll; dispatch mouse click
            cdp('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': x, 'y': y})
            cdp('Input.dispatchMouseEvent', {'type': 'mousePressed', 'x': x, 'y': y, 'button': 'left', 'clickCount': 1})
            cdp('Input.dispatchMouseEvent', {'type': 'mouseReleased', 'x': x, 'y': y, 'button': 'left', 'clickCount': 1})
            return None

    if scroll_err:
        scroll_fail_desc = f'scroll error: {scroll_err}'
    elif rect:
        scroll_fail_desc = f'zero box ({rect.get("width", 0):.0f}x{rect.get("height", 0):.0f})'
    else:
        scroll_fail_desc = 'no box after scroll'

    # Step 2: DOM click (+ Step 3: Form submit if submit control inside a form and click no-ops)
    step2_3_js = """function() {
        let clickOk = false;
        let clickErr = null;
        let submitFired = false;
        const form = this.form || (typeof this.closest === 'function' ? this.closest('form') : null);
        const isSubmit = (this.type === 'submit') ||
                         (this.tagName === 'BUTTON' && (!this.type || this.type === 'submit')) ||
                         (this.getAttribute && this.getAttribute('type') === 'submit');

        let submitHandler = null;
        if (form) {
            submitHandler = () => { submitFired = true; };
            try {
                form.addEventListener('submit', submitHandler, { capture: true, once: true });
            } catch (e) {}
        }

        try {
            if (typeof this.click === 'function') {
                this.click();
                clickOk = true;
            } else {
                clickErr = 'node has no click() method';
            }
        } catch (e) {
            clickErr = (e && e.message) ? e.message : String(e);
        }

        let formSubmitOk = false;
        let formSubmitErr = null;
        if (form && isSubmit && !submitFired) {
            try {
                if (typeof form.requestSubmit === 'function') {
                    form.requestSubmit(this);
                    formSubmitOk = true;
                } else if (typeof form.submit === 'function') {
                    form.submit();
                    formSubmitOk = true;
                }
            } catch (e) {
                formSubmitErr = (e && e.message) ? e.message : String(e);
            }
        }

        if (form && submitHandler) {
            try {
                form.removeEventListener('submit', submitHandler, { capture: true });
            } catch (e) {}
        }

        return {
            clickOk: clickOk,
            clickErr: clickErr,
            isSubmit: Boolean(isSubmit),
            hasForm: Boolean(form),
            submitFired: submitFired,
            formSubmitOk: formSubmitOk,
            formSubmitErr: formSubmitErr
        };
    }"""
    call2 = cdp_send('Runtime.callFunctionOn', {
        'objectId': oid,
        'functionDeclaration': step2_3_js,
        'returnByValue': True
    })
    s2_data = call2.get('result', {}).get('result', {}).get('value', {})
    if not isinstance(s2_data, dict):
        ex = call2.get('result', {}).get('exceptionDetails', {}).get('exception', {}).get('description')
        err_msg = ex or call2.get('error', {}).get('message', 'DOM click evaluation failed')
        return f'no geometry; scroll+retry failed ({scroll_fail_desc}); DOM click raised {err_msg}'

    click_ok = s2_data.get('clickOk', False)
    click_err = s2_data.get('clickErr')
    is_submit = s2_data.get('isSubmit', False)
    has_form = s2_data.get('hasForm', False)
    submit_fired = s2_data.get('submitFired', False)
    form_submit_ok = s2_data.get('formSubmitOk', False)
    form_submit_err = s2_data.get('formSubmitErr')

    if click_ok or form_submit_ok:
        return None

    errors = [f'no geometry; scroll+retry failed ({scroll_fail_desc})']
    if click_err:
        errors.append(f'DOM click raised {click_err}')
    if has_form and is_submit and form_submit_err:
        errors.append(f'form submit raised {form_submit_err}')
    return '; '.join(errors)

@mcp.tool()
def glazyr_status() -> str:
    """Bridge and browser health: current URL/title, state sequence,
    command-channel (CDP) health, last error if any, tokens served.
    Reads still work when the command channel is down - this is how
    you tell the two channels apart."""
    targets = _cdp_targets()
    cdp_ok = targets is not None
    state_ok = refresh_apc()
    url, title = page_info() if state_ok else ('', '')
    lines = ['# glazyr status',
             f'url: {url or "(none)"}',
             f'title: {title or "(none)"}',
             f'seq: {_state["seq"]}',
             f'state_channel (shared memory): {"live" if state_ok else "no state"}',
             f'command_channel (CDP): {"ok" if cdp_ok else "down"}',
             f'reads: {_state["reads"]}  tokens_served_total: {_state["tokens_served"]}']
    # v0.2.3: report channel agreement honestly. During the Run 4
    # desync this tool claimed both channels "ok" while they pointed
    # at different pages; now it counts page targets and checks the
    # bound target against the state channel's URL.
    if targets is not None:
        pages = [t for t in targets if t.get('type') == 'page']
        lines.append(f'page_targets: {len(pages)}')
        bound = next((t for t in pages
                      if t.get('id') == _state['bound_target']), None)
        if bound is not None and url:
            if bound.get('url') == url:
                lines.append('command/state target: agree')
            else:
                lines.append(
                    f'command/state target: DESYNC (commands bound to '
                    f'{bound.get("url")}, state shows {url})')
        if len(pages) > 1:
            lines.append('# note: multiple page targets open; commands '
                         'bind to the state channel\'s page.')
    if _state['seq_at']:
        lines.append(f'state_age_s: {round(time.time() - _state["seq_at"])}')
    # v0.3.2: producer liveness + resident identity, reported honestly.
    health = producer_health()
    lines.append(f'producer: {health["status"]} ({health["detail"]})')
    if _state['producer_died']:
        d = _state['producer_died']
        lines.append(f'producer_died: at seq {d["seq"]} on '
                     f'{d["url"] or "(no page)"} - {d["detail"]}')
    res = _state['resident']
    if res:
        lines.append(f'bridge_mode: resident (pid {os.getpid()}, '
                     f'uptime_s: {round(time.time() - res["started_at"])}, '
                     f'sessions_served: {res["sessions"]})')
    else:
        lines.append('bridge_mode: per-connection (stdio)')
    if _state['last_error']:
        lines.append(f'last_error: {_state["last_error"]}')
    return '\n'.join(lines)

@mcp.tool()
def glazyr_peek(level: str = 'standard', force: bool = False) -> str:
    """Current page state from Glazyr shared memory, filtered.
    level: light | standard | aggressive. Node ids in [brackets] are the
    handles for glazyr_click / glazyr_type. If nothing changed since the
    last read you get a short stub instead of the full state; pass
    force=true to resend it."""
    return do_peek(level, force=force)

@mcp.tool()
def glazyr_peek_raw() -> str:
    """Unfiltered page state (merged serialization, no elision). Use when
    the filtered view is missing something; costs more tokens."""
    return do_peek('none')

@mcp.tool()
def glazyr_peek_subtree(node_id: int, level: str = 'standard') -> str:
    """Serialize just one node's subtree - the targeted way to recover a
    truncated paragraph or inspect one section without a raw read."""
    ensure_browser()
    err = _guard_state_fresh()
    if err:
        return err
    node, note = resolve_node(node_id)
    if node is None:
        return note or f'Glazyr: node {node_id} not in current page state. Peek again.'
    t0 = time.perf_counter()
    overlay = _state['overlay'] if level != 'none' else None
    text, _st = gf.serialize_subtree(_state['apc'], node, level, overlay)
    read_ms = round((time.perf_counter() - t0) * 1000)
    _mark_served()
    hdr = _header()
    if hdr:
        text = hdr + '\n' + text
    if note:
        text += '\n' + note
    toks = count_tokens(text)
    _state['tokens_served'] += toks
    return text + '\n' + _footer(toks, read_ms, level)

@mcp.tool()
def glazyr_find(query: str, limit: int = 20) -> str:
    """Find nodes whose text, label, URL, or form field name/value
    contains the query (case-insensitive, phrase-exact substring).
    Heading and paragraph text is searched too, and a match against
    the page title is reported. Returns matching nodes with their
    ids - use it to target a peek_subtree or a click without reading
    the whole page."""
    ensure_browser()
    if not refresh_apc():
        return 'Glazyr: no page state yet. Navigate somewhere first.'
    err = _guard_state_fresh()
    if err:
        return err
    t0 = time.perf_counter()
    matches = gf.find_nodes(_state['apc'], query, limit)
    read_ms = round((time.perf_counter() - t0) * 1000)
    _mark_served()
    lines = [_header(), f'# find {query!r}: {len(matches)} match(es)']
    if _state['page_title'] and query.lower() in _state['page_title'].lower():
        lines.append(f'# page title also matches: {_state["page_title"]!r}')
    for n in matches:
        dom = n.content_attributes.common_ancestor_dom_node_id
        rendered = gf.render_node(_state['apc'], n, 'standard',
                                  _state['overlay'])
        if f'[{dom}]' not in rendered:
            tname = T.get(n.content_attributes.attribute_type, '?')
            rendered = f'[{dom}] {tname}\n' + rendered
        lines.append(rendered)
    text = '\n'.join(l for l in lines if l)
    toks = count_tokens(text)
    _state['tokens_served'] += toks
    return text + '\n' + _footer(toks, read_ms, 'find')

def _url_eq(a, b):
    """URL equality tolerant of a lone trailing slash (navigating to
    https://example.com lands as https://example.com/)."""
    if not a or not b:
        return False
    return a == b or a == b + '/' or a + '/' == b

def _navigate_attempt(url, wait_s=45):
    """One navigate + confirm cycle. Returns True only when the state
    in hand is verifiably the target document - a late producer write
    from the PREVIOUS page can satisfy the seq wait with the wrong
    document, so confirmation requires a new document on the target
    host, or the state URL already being the target (same-document
    and anchor navigations create no new document)."""
    refresh_apc()
    old_seq = _state['seq']
    old_doc = _state['doc_key']
    cdp('Page.enable')
    cdp('Page.navigate', {'url': url})
    wait_for_new_seq(old_seq, timeout=wait_s)
    target_host = urlparse(url).hostname
    deadline = time.time() + 12
    while True:
        refresh_apc()
        if _url_eq(_state['page_url'], url):
            return True
        cur_host = urlparse(_state['page_url']).hostname
        if _state['doc_key'] is None or \
                (_state['doc_key'] != old_doc and cur_host == target_host):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.25)

@mcp.tool()
def glazyr_navigate(url: str, level: str = 'standard', full: bool = False) -> str:
    """Navigate the Glazyr browser to a URL. By default returns a
    summary (URL, title, node counts, heading outline) - verified to be
    the target page, not a late write from the previous one. Use
    glazyr_peek / glazyr_find / glazyr_peek_subtree to read the page;
    pass full=true to get the full state at the given level directly,
    as before.

    v0.2.3: if the navigation cannot be confirmed against the state
    channel (dead renderer, wedged target), the browser is relaunched
    and the navigation retried once; if it still cannot be confirmed,
    the tool returns an explicit error - it never serves the previous
    page as if the navigation had succeeded."""
    ensure_browser()
    recovered = False
    try:
        landed = _navigate_attempt(url)
    except Exception:
        landed = False
    if not landed:
        _state['last_error'] = (
            f'navigate to {url} unconfirmed (state still shows '
            f'{_state["page_url"] or "(none)"}); relaunched the '
            f'browser and retried')
        _force_relaunch()
        recovered = True
        try:
            landed = _navigate_attempt(url, wait_s=30)
        except Exception:
            landed = False
        if not landed:
            _state['last_error'] = (
                f'navigate to {url} failed after a browser relaunch; '
                f'state shows {_state["page_url"] or "(none)"}')
            return (f'Glazyr: navigation to {url} did not land. The '
                    f'browser was relaunched and the navigation '
                    f'retried, but the state channel still shows '
                    f'{_state["page_url"] or "(no page)"}. Nothing '
                    f'from the wrong page was served. Check '
                    f'glazyr_status and try again.')
    if full:
        out = do_peek(level, force=True)
    else:
        out = page_summary()
    if recovered:
        out = ('# note: the first navigation attempt did not land '
               '(browser wedged); the browser was relaunched and the '
               'navigation retried successfully.\n' + out)
    return out

@mcp.tool()
def glazyr_click(node_id: int, level: str = 'standard') -> str:
    """Click the element with the given node id (from glazyr_peek output).
    If the click navigates, returns the new page state; if not, returns a
    compact ack instead of resending unchanged state."""
    ensure_browser()
    err = _guard_state_fresh()
    if err:
        return err
    node, note = resolve_node(node_id)
    if node is None:
        return note or f'Glazyr: node {node_id} not in current page state. Peek again.'
    real_id = node.content_attributes.common_ancestor_dom_node_id
    old_seq = _state['seq']
    old_doc = _state['doc_key']
    err = _click_with_fallback(node, node_id)
    if err:
        return f'Glazyr: node {node_id}: {err}.'
    # Bridge-asserted overlay for form controls: radios/checkboxes get a
    # CHECKED annotation the agent can see in the next peek (the producer
    # itself only writes state on navigation). v0.2.2: the asserted
    # delta also rides in the ack itself, so verifying a click no longer
    # costs a peek round-trip; checkboxes toggle honestly.
    delta = None
    a = node.content_attributes
    if T.get(a.attribute_type) == 'FORM_CONTROL':
        fc = a.form_control_data
        fct = cq.FormControlType.Name(fc.form_control_type)
        if 'RADIO' in fct:
            for oid, osig in list(_state['sigs'].items()):
                if osig.get('name') == fc.field_name and 'RADIO' in osig.get('fctype', ''):
                    if oid in _state['overlay']:
                        _state['overlay'][oid].pop('checked', None)
            _overlay_set(real_id, 'checked', 'CHECKED (bridge-asserted)')
            delta = 'asserted CHECKED'
        elif 'CHECKBOX' in fct:
            if fc.is_checked:
                _overlay_set(real_id, 'checked', 'UNCHECKED (bridge-asserted)')
                delta = 'asserted UNCHECKED'
            else:
                _overlay_set(real_id, 'checked', 'CHECKED (bridge-asserted)')
                delta = 'asserted CHECKED'
    is_anchor = T.get(a.attribute_type) == 'ANCHOR'
    is_submit = False
    if T.get(a.attribute_type) == 'FORM_CONTROL':
        fc = a.form_control_data
        fct = cq.FormControlType.Name(fc.form_control_type)
        if 'SUBMIT' in fct:
            is_submit = True

    # Navigation = a new document. A same-document producer rewrite
    # (state refreshed, same identifier) is not a navigation.
    # For navigations (anchors/submits), continue waiting if a same-document
    # write arrived before the new document landed.
    deadline = time.time() + (12 if (is_anchor or is_submit) else 2.5)
    changed = False
    while time.time() < deadline:
        wait_for_new_seq(_state['seq'], timeout=max(0.5, deadline - time.time()))
        changed = (_state['doc_key'] != old_doc) if old_doc is not None \
            else (_state['seq'] != old_seq)
        if changed or not (is_anchor or is_submit):
            break
    if changed:
        out = do_peek(level, force=True)
        return (note + '\n' + out) if note else out
    ack = (f'Glazyr: clicked {_node_desc(real_id, node)}'
           + (f' → {delta} (bridge-asserted)' if delta else '')
           + f'. No navigation followed; page state unchanged '
             f'(seq={_state["seq"]}).'
           + ('' if delta else ' Any control state this click set is '
              'annotated (bridge-asserted) in the next peek.'))
    if note:
        ack = note + '\n' + ack
    toks = count_tokens(ack)
    _state['tokens_served'] += toks
    return ack + f'\n# glazyr tokens_this_read={toks} tokens_served_total={_state["tokens_served"]}'

@mcp.tool()
def glazyr_type(node_id: int, text: str) -> str:
    """Focus the element with the given node id and type text into it.
    The value is annotated (bridge-asserted) in subsequent peeks."""
    ensure_browser()
    err = _guard_state_fresh()
    if err:
        return err
    node, note = resolve_node(node_id)
    if node is None:
        return note or f'Glazyr: node {node_id} not in current page state. Peek again.'
    real_id = node.content_attributes.common_ancestor_dom_node_id
    err = _click_node(node)
    if err:
        return f'Glazyr: node {node_id}: {err}.'
    cdp('Input.insertText', {'text': text})
    _overlay_set(real_id, 'value', f'value={text!r} (bridge-asserted)')
    ack = (f'Glazyr: typed {len(text)} chars into {_node_desc(real_id, node)} '
           f'→ value={text!r} (bridge-asserted). Page state confirms it '
           f'after the next navigation.')
    if note:
        ack = note + '\n' + ack
    return ack

@mcp.tool()
def glazyr_scroll(direction: str = 'down', amount: int = 600,
                  level: str = 'standard') -> str:
    """Scroll the page down (or up) by amount pixels. Returns new state
    at the given level (or an unchanged stub - the v0 producer writes
    state on load, so scrolling does not refresh the tree)."""
    ensure_browser()
    refresh_apc()
    old_seq = _state['seq']
    old_doc = _state['doc_key']
    dy = amount if direction == 'down' else -amount
    cdp('Input.dispatchMouseEvent', {'type': 'mouseWheel', 'x': 400, 'y': 300, 'deltaX': 0, 'deltaY': dy})
    wait_for_new_seq(old_seq, timeout=5)
    changed = (_state['doc_key'] != old_doc) if old_doc is not None \
        else (_state['seq'] != old_seq)
    out = do_peek(level, force=changed)
    if not changed:
        out += '\n# note: scroll did not trigger a state refresh in v0 (producer writes on load); geometry in the previous state remains valid for clicks.'
    return out

# ---------- resident mode (v0.3.2) ----------
def _glazyr_home():
    return os.environ.get('GLAZYR_HOME') or os.path.join(
        os.path.expanduser('~'), '.glazyr')

def _package_version():
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               '..', 'package.json')) as f:
            return json.load(f).get('version', 'unknown')
    except Exception:
        return 'unknown'

def _run_resident():
    """Single-instance resident bridge: serves MCP sessions over a
    Unix socket under ~/.glazyr, one browsing session shared by every
    attached client (v1 semantics, per scope). Started detached by
    the launcher; exits after GLAZYR_RESIDENT_IDLE_S seconds (default
    900) with no connection attached. Owns its browser: on exit the
    browser goes with it - no orphans, ever."""
    import asyncio
    import anyio
    from mcp.shared.message import SessionMessage
    from mcp.types import CallToolRequest, JSONRPCMessage

    home = _glazyr_home()
    os.makedirs(home, exist_ok=True)
    sock_path = os.path.join(home, 'resident.sock')
    json_path = os.path.join(home, 'resident.json')
    hb_path = os.path.join(home, 'resident.heartbeat')
    lock_path = os.path.join(home, 'resident.lock')
    idle_s = int(os.environ.get('GLAZYR_RESIDENT_IDLE_S', '900'))

    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Another resident owns the instance lock; it is the server.
        print('[glazyr] resident already running; exiting', flush=True)
        return

    _state['resident'] = {'started_at': time.time(),
                          'last_activity': time.time(),
                          'connections': 0, 'sessions': 0}

    def touch():
        _state['resident']['last_activity'] = time.time()

    # Serialize tool calls across sessions: one resident = one
    # browsing session, and the CDP websocket / _state are shared.
    _tool_lock = anyio.Lock()
    _orig_call = mcp._mcp_server.request_handlers.get(CallToolRequest)

    async def _locked_call(request):
        async with _tool_lock:
            touch()
            return await _orig_call(request)
    if _orig_call is not None:
        mcp._mcp_server.request_handlers[CallToolRequest] = _locked_call

    async def handle(reader, writer):
        res = _state['resident']
        res['connections'] += 1
        res['sessions'] += 1
        touch()
        read_send, read_recv = anyio.create_memory_object_stream(100)
        write_send, write_recv = anyio.create_memory_object_stream(100)

        async def pump_in():
            try:
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = JSONRPCMessage.model_validate_json(line)
                    except Exception as exc:
                        await read_send.send(exc)
                        continue
                    await read_send.send(SessionMessage(msg))
            except Exception:
                pass
            finally:
                try:
                    await read_send.aclose()
                except Exception:
                    pass

        async def pump_out():
            try:
                async for msg in write_recv:
                    data = (msg.message.root.model_dump_json(
                        by_alias=True, exclude_none=True) + '\n')
                    writer.write(data.encode())
                    await writer.drain()
            except Exception:
                pass

        sess = asyncio.ensure_future(mcp._mcp_server.run(
            read_recv, write_send,
            mcp._mcp_server.create_initialization_options(),
            raise_exceptions=False))
        pin = asyncio.ensure_future(pump_in())
        pout = asyncio.ensure_future(pump_out())
        try:
            await asyncio.wait([sess, pin],
                                 return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (sess, pin, pout):
                t.cancel()
            try:
                writer.close()
            except Exception:
                pass
            res['connections'] -= 1
            touch()

    async def main():
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass
        server = await asyncio.start_unix_server(handle, path=sock_path)
        with open(json_path, 'w') as f:
            json.dump({'pid': os.getpid(), 'sock': sock_path,
                       'version': _package_version(),
                       'started_at': time.time()}, f)
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig_name in ('SIGTERM', 'SIGINT'):
            try:
                loop.add_signal_handler(getattr(__import__('signal'),
                                                sig_name), stop.set)
            except Exception:
                pass

        def heartbeat():
            while not stop.is_set():
                try:
                    with open(hb_path, 'w') as f:
                        f.write(str(time.time()))
                except Exception:
                    pass
                res = _state['resident']
                if res['connections'] == 0 and \
                        time.time() - res['last_activity'] > idle_s:
                    loop.call_soon_threadsafe(stop.set)
                    return
                time.sleep(5)
        threading.Thread(target=heartbeat, daemon=True).start()

        print(f'[glazyr] resident bridge up (pid {os.getpid()}, '
              f'socket {sock_path}, idle timeout {idle_s}s)', flush=True)
        async with server:
            await stop.wait()
        # Clean shutdown: our browser goes with us, rendezvous files
        # are removed, the instance lock releases on process exit.
        popen = _state['browser']
        if popen is not None and popen.poll() is None:
            try:
                os.killpg(popen.pid, 15)
            except Exception:
                try:
                    popen.terminate()
                except Exception:
                    pass
        for f in (sock_path, json_path, hb_path):
            try:
                os.unlink(f)
            except Exception:
                pass
        print('[glazyr] resident bridge exiting', flush=True)

    anyio.run(main)

if __name__ == '__main__':
    if '--resident' in sys.argv:
        _run_resident()
    else:
        mcp.run()
