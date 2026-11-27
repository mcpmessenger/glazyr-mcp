"""Glazyr Trace - writer and reader for TRACE_FORMAT v1 (Draft v0.4).

Writer (section 3/4/6 of TRACE_FORMAT.md): JSON Lines, one serialized
append per event under one lock, RFC 8785 (JCS) canonicalization +
SHA-256 hash chain from the empty-string genesis, write-ahead actions,
lazy per-session session_start, a fresh file per writer instance (never
appending to a file it did not create) linked back with
session_start.continues.

Reader (section 6 / 13): streaming chain verification with one-line
lookahead (verified prefix, break at n=k, torn tail), mechanical
conformance checks, and the glazyr_trace views (summary / tail /
failures / url / since / verify) under the section 13.4 cost law.

No third-party dependency except `rfc8785` - canonicalization must not
be hand-rolled (section 6).
"""
import collections
import datetime
import hashlib
import json
import os
import re
import threading

import rfc8785

FORMAT_V = 1
GENESIS = hashlib.sha256(b'').hexdigest()
MAX_INT = 2 ** 53 - 1
TRACE_SUFFIX = '.trace.jsonl'

_SURROGATE = re.compile('[\ud800-\udfff]')
_TS_RE = re.compile(r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$')
_NAME_PID = re.compile(r'^glazyr-(\d+)-')
_PROV_RANK = {'inferred': 0, 'asserted': 1, 'observed': 2}


def weakest(*provs):
    """The weakest of several provenance values (section 7). Ranked
    observed > asserted > inferred: an inference is the least direct
    claim, which is also why a re-matched target *caps* at inferred."""
    provs = [p for p in provs if p]
    return min(provs, key=lambda p: _PROV_RANK[p]) if provs else 'asserted'


def now_ts():
    t = datetime.datetime.now(datetime.timezone.utc)
    return t.strftime('%Y-%m-%dT%H:%M:%S.') + f'{t.microsecond // 1000:03d}Z'


def sanitize(v):
    """Enforce the section 3 data constraints on emitted values:
    absent-not-null (None members are dropped), integers only (floats
    rounded, out-of-range dropped), lone surrogates -> U+FFFD."""
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            x = sanitize(x)
            if x is not None:
                out[str(k)] = x
        return out
    if isinstance(v, (list, tuple)):
        return [x for x in (sanitize(i) for i in v) if x is not None]
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v if 0 <= v <= MAX_INT else None
    if isinstance(v, float):
        if v != v or v in (float('inf'), float('-inf')):
            return None
        return sanitize(int(round(v)))
    if isinstance(v, str):
        return _SURROGATE.sub('\ufffd', v)
    if v is None:
        return None
    return sanitize(str(v))


def hash_obj(obj):
    return hashlib.sha256(rfc8785.dumps(obj)).hexdigest()


def hash_line(line):
    """Section 6: parse the line, JCS-canonicalize every member
    (prev_hash included), UTF-8, SHA-256."""
    return hash_obj(json.loads(line))


# ---------------------------------------------------------------------
# writer
# ---------------------------------------------------------------------

def _pid_is_live_bridge(pid):
    try:
        with open(f'/proc/{pid}/cmdline', 'rb') as f:
            return b'glazyr_mcp' in f.read()
    except Exception:
        return False


def find_predecessor(trace_dir, exclude=None):
    """The most recent trace file in trace_dir whose writer is no
    longer running - the file a restarted emitter continues. Files
    whose writer pid is a live bridge belong to a concurrent instance
    and are never claimed."""
    try:
        names = [n for n in os.listdir(trace_dir) if n.endswith(TRACE_SUFFIX)]
    except Exception:
        return None
    cands = []
    for n in names:
        p = os.path.join(trace_dir, n)
        if exclude and os.path.abspath(p) == os.path.abspath(exclude):
            continue
        m = _NAME_PID.match(n)
        if m and int(m.group(1)) != os.getpid() and \
                _pid_is_live_bridge(int(m.group(1))):
            continue
        try:
            cands.append((os.path.getmtime(p), n, p))
        except Exception:
            pass
    if not cands:
        return None
    return max(cands)[2]


class TraceWriter:
    """One writer per file, one file per instance. Thread-safe: the
    tool path and the resident heartbeat thread share self.lock, held
    across "read head hash, append line" (section 3)."""

    def __init__(self, trace_dir, instance, emitter, capabilities,
                 stack='glazyr', tokenizer=None):
        self.lock = threading.RLock()
        self.dir = trace_dir
        self.instance = instance
        self.emitter = emitter
        self.capabilities = capabilities
        self.stack = stack
        self.tokenizer = tokenizer
        self.path = None
        self.fd = None
        self.n = 0
        self.head = GENESIS
        self.disabled = None
        self.sessions = {}        # session -> totals (this file only)
        self._continues_done = False
        self.continued_from = None

    # -- file ------------------------------------------------------
    def _open(self):
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        pred = find_predecessor(self.dir)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime(
            '%Y%m%d-%H%M%S')
        base = f'{self.instance}-{stamp}'
        for i in range(100):
            name = base + (f'-{i}' if i else '') + TRACE_SUFFIX
            path = os.path.join(self.dir, name)
            try:
                # O_EXCL: a writer never appends to a file it did not
                # create (section 3).
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                             os.O_APPEND, 0o600)
            except FileExistsError:
                continue
            self.path, self.fd = path, fd
            if pred:
                rep = verify_file(pred)
                if rep['exists'] and rep['verified'] > 0:
                    self.continued_from = {'file': pred,
                                           'head_hash': rep['head']}
            return
        raise OSError('could not create a fresh trace file')

    def _write_line(self, line):
        data = (line + '\n').encode('utf-8')
        view = memoryview(data)
        while view:
            k = os.write(self.fd, view)
            view = view[k:]

    # -- events ----------------------------------------------------
    def _append_locked(self, session, kind, provenance, fields):
        if self.fd is None:
            self._open()
        ev = {'v': FORMAT_V, 'n': self.n + 1, 'ts': now_ts(), 'kind': kind,
              'session': session, 'prev_hash': self.head,
              'provenance': provenance}
        for k, x in sanitize(fields or {}).items():
            if k not in ev:
                ev[k] = x
        line = json.dumps(ev, ensure_ascii=False, allow_nan=False,
                          separators=(',', ':'))
        self._write_line(line)
        # Hash the line actually written, parsed back (section 6).
        self.head = hash_line(line)
        self.n += 1
        self._account(session, ev)
        return ev['n']

    def _account(self, session, ev):
        t = self.sessions.setdefault(session, {
            'reads': 0, 'actions': 0, 'failures': 0, 'tokens': 0,
            'ended': False})
        k = ev['kind']
        if k == 'state_read':
            t['reads'] += 1
            if 'error' in ev:
                t['failures'] += 1
            tok = ev.get('tokens')
            if tok:
                t['tokens'] += tok.get('count', 0)
        elif k == 'action':
            t['actions'] += 1
        elif k == 'action_outcome' and \
                ev.get('outcome', {}).get('status') == 'error':
            t['failures'] += 1

    def _ensure_session(self, session):
        if session in self.sessions:
            return
        fields = {'emitter': self.emitter, 'stack': self.stack,
                  'capabilities': self.capabilities}
        if self.fd is None:
            self._open()
        if not self._continues_done:
            self._continues_done = True
            if self.continued_from:
                fields['continues'] = self.continued_from
        self._append_locked(session, 'session_start', 'asserted', fields)

    def append(self, session, kind, provenance, fields=None):
        """Append one event; returns its n, or None if tracing is off.
        A trace failure never fails the tool call that caused it: the
        writer disables itself and says why (glazyr_trace reports it),
        rather than risk appending after a partial line."""
        with self.lock:
            if self.disabled:
                return None
            try:
                self._ensure_session(session)
                return self._append_locked(session, kind, provenance, fields)
            except Exception as e:
                self.disabled = f'{type(e).__name__}: {e}'
                return None

    def session_end(self, session, reason):
        with self.lock:
            t = self.sessions.get(session)
            if self.disabled or t is None or t['ended']:
                return None
            totals = {'reads': t['reads'], 'actions': t['actions'],
                      'failures': t['failures']}
            if self.tokenizer:
                totals['tokens'] = {'count': t['tokens'],
                                    'tokenizer': self.tokenizer}
            n = self.append(session, 'session_end', 'asserted',
                            {'reason': reason, 'totals': totals})
            t['ended'] = True
            return n


# ---------------------------------------------------------------------
# reader: streaming verification
# ---------------------------------------------------------------------

class _Unparseable(Exception):
    pass


def _reject_constant(c):
    raise _Unparseable(f'non-JSON constant {c}')


def parse_line(body, line_no):
    """Strict parse of one line body (bytes, LF and one CR removed).
    Returns the object, or raises _Unparseable(reason)."""
    if not body:
        raise _Unparseable('blank line')
    if line_no == 1 and body.startswith(b'\xef\xbb\xbf'):
        raise _Unparseable('UTF-8 BOM')
    try:
        s = body.decode('utf-8')
    except UnicodeDecodeError:
        raise _Unparseable('invalid UTF-8')
    dups = []

    def hook(pairs):
        seen = set()
        for k, _ in pairs:
            if k in seen:
                dups.append(k)
            seen.add(k)
        return dict(pairs)
    try:
        obj = json.loads(s, object_pairs_hook=hook,
                         parse_constant=_reject_constant)
    except _Unparseable:
        raise
    except ValueError as e:
        raise _Unparseable(f'invalid JSON ({e.msg})')
    if dups:
        raise _Unparseable(f'duplicate key {dups[0]!r}')
    if not isinstance(obj, dict):
        raise _Unparseable('line is not a JSON object')
    if _has_surrogate(obj):
        raise _Unparseable('lone-surrogate escape (hash not well-defined)')
    return obj


def _has_surrogate(v):
    if isinstance(v, str):
        return bool(_SURROGATE.search(v))
    if isinstance(v, dict):
        return any(_has_surrogate(k) or _has_surrogate(x) for k, x in v.items())
    if isinstance(v, list):
        return any(_has_surrogate(x) for x in v)
    return False


def _conformance(obj, line_no):
    """Mechanical reader checks for a v1 event (section 3/4)."""
    out = []

    def walk(v, path):
        if v is None:
            out.append(f'null value at {path or "<root>"}')
        elif isinstance(v, bool):
            pass
        elif isinstance(v, float):
            out.append(f'non-integer number at {path}')
        elif isinstance(v, int):
            if not 0 <= v <= MAX_INT:
                out.append(f'integer out of range at {path}')
        elif isinstance(v, dict):
            for k, x in v.items():
                walk(x, f'{path}.{k}' if path else k)
        elif isinstance(v, list):
            for i, x in enumerate(v):
                walk(x, f'{path}[{i}]')
    walk(obj, '')
    if obj.get('n') != line_no:
        out.append(f'n={obj.get("n")} on line {line_no}')
    ts = obj.get('ts')
    if not isinstance(ts, str) or not _TS_RE.match(ts):
        out.append(f'ts not in RFC 3339 ms-Z form: {ts!r}')
    for f in ('kind', 'session', 'prev_hash', 'provenance'):
        if f not in obj:
            out.append(f'missing common field {f}')
    return out


def _iter_lines(f):
    """Yield (line_no, body, has_lf, is_last) with one-line lookahead."""
    line_no = 0
    cur = f.readline()
    while cur:
        nxt = f.readline()
        line_no += 1
        has_lf = cur.endswith(b'\n')
        body = cur[:-1] if has_lf else cur
        if body.endswith(b'\r'):
            body = body[:-1]
        yield line_no, body, has_lf, not nxt
        cur = nxt


def verify_file(path, on_event=None, stop_after=None):
    """Recompute the chain (section 6). on_event(obj) is called for each
    event of the verified prefix, in order, once its membership is
    settled. Returns the report dict."""
    rep = {'file': path, 'exists': False, 'empty': False, 'chain': 'ok',
           'break_at': None, 'break_reason': None, 'verified': 0,
           'head': None, 'violations': [], 'warnings': [], 'torn': None,
           'max_v': 0, 'lines': 0}
    try:
        f = open(path, 'rb')
    except OSError:
        return rep
    rep['exists'] = True
    with f:
        pending = None            # (obj, line_no, hash)
        expected = GENESIS

        def commit(p):
            obj, ln, h = p
            rep['verified'] = ln
            rep['head'] = h
            v = obj.get('v')
            if isinstance(v, int) and v > rep['max_v']:
                rep['max_v'] = v
            if v == FORMAT_V:
                for msg in _conformance(obj, ln):
                    _add_violation(rep, f'n={obj.get("n", ln)}: {msg}')
            if on_event is not None:
                on_event(obj)

        for line_no, body, has_lf, is_last in _iter_lines(f):
            rep['lines'] = line_no
            try:
                obj = parse_line(body, line_no)
                h = hash_obj(obj)
            except _Unparseable as e:
                if is_last and not has_lf:
                    rep['chain'] = 'torn tail'
                    rep['torn'] = {'line': line_no, 'reason': str(e)}
                else:
                    rep['chain'] = 'break'
                    rep['break_at'] = line_no
                    rep['break_reason'] = f'line {line_no} unparseable: {e}'
                    _add_violation(rep, f'line {line_no}: {e}')
                break
            except Exception as e:   # canonicalization failure
                rep['chain'] = 'break'
                rep['break_at'] = line_no
                rep['break_reason'] = (f'line {line_no} cannot be '
                                       f'canonicalized: {e}')
                _add_violation(rep, rep['break_reason'])
                break
            if obj.get('prev_hash') != expected:
                rep['chain'] = 'break'
                if pending is None:
                    rep['break_at'] = 1
                    rep['break_reason'] = 'line 1 prev_hash is not the genesis hash'
                else:
                    rep['break_at'] = pending[1]
                    rep['break_reason'] = (
                        f'line {line_no} prev_hash does not match the hash '
                        f'of line {pending[1]}')
                    pending = None   # its outgoing link failed
                break
            if pending is not None:
                commit(pending)
                if stop_after and rep['verified'] >= stop_after:
                    pending = None
                    break
            pending = (obj, line_no, h)
            expected = h
            if is_last and not has_lf:
                rep['warnings'].append('unterminated final line')
        if pending is not None:
            commit(pending)
        if rep['lines'] == 0:
            rep['empty'] = True
    return rep


def _add_violation(rep, msg):
    if len(rep['violations']) < 50:
        rep['violations'].append(msg)


def chain_label(rep):
    if rep['chain'] == 'break':
        return f'break at n={rep["break_at"]}'
    return rep['chain']


# ---------------------------------------------------------------------
# reader: views (section 13)
# ---------------------------------------------------------------------

_TOGGLE = ('CHECKBOX', 'RADIO', 'BUTTON')


def _is_assertion(ev):
    """Pending-fill candidates (section 5.5): type actions and toggle
    clicks (target is a checkbox, radio or button control)."""
    if ev.get('action') == 'type':
        return True
    if ev.get('action') == 'click':
        sig = (ev.get('target') or {}).get('sig') or {}
        fct = str(sig.get('fctype', '')) + ' ' + str(sig.get('role', ''))
        return any(t in fct.upper() for t in _TOGGLE)
    return False


def _short(h, k=12):
    return h[:k] if isinstance(h, str) else '?'


def _target_str(t):
    """A target, signature never cut (section 13.4)."""
    if not t:
        return ''
    s = t.get('desc', '')
    rid, aid = t.get('requested_id'), t.get('id')
    if rid is not None and aid is not None and rid != aid:
        s += f' (re-matched {rid}->{aid})'
    sig = t.get('sig')
    if sig:
        s += ' sig=' + json.dumps(sig, ensure_ascii=False,
                                  separators=(',', ':'), sort_keys=True)
    return s


def _act_str(a):
    s = f'n={a["n"]} {a.get("action")}'
    if a.get('cause') and a.get('cause') != 'agent':
        s += f' [{a["cause"]}]'
    if a.get('url'):
        s += f' {a["url"]}'
    if a.get('target'):
        s += ' ' + _target_str(a['target'])
    if a.get('action') == 'type':
        if a.get('value_redacted'):
            s += ' value=<redacted>'
        elif 'value' in a:
            s += f' value={a["value"]!r}'
    if a.get('direction'):
        s += f' {a["direction"]}'
    return s


def _outcome_str(o):
    if o is None:
        return 'INTERRUPTED (no outcome)'
    oc = o.get('outcome', {})
    s = oc.get('status', '?')
    if oc.get('status') == 'error':
        s += f' error={oc.get("error", "")!r}'
    if 'navigated' in oc:
        s += f' navigated={json.dumps(oc["navigated"])}'
    if oc.get('method'):
        s += f' method={oc["method"]}'
    if oc.get('new_url'):
        s += f' -> {oc["new_url"]}'
    s += f' ({o.get("provenance")})'
    return s


class Model:
    """Single streaming pass over a verified prefix, building only the
    aggregates the views need (section 3: readers stream)."""

    def __init__(self, scope_session, mode, url=None, since=None,
                 n_actions=10):
        self.scope = scope_session      # None = all sessions
        self.mode = mode
        self.url = url
        self.since = since
        self.sessions = collections.OrderedDict()
        self.kinds = collections.Counter()
        self.reads = self.actions = self.failures = 0
        self.tokens = collections.Counter()      # tokenizer -> count
        self.goal = None
        self.goal_any = None
        self.position = None
        self.prev_position = None
        self.open_actions = collections.OrderedDict()   # n -> action
        self.outcomes = {}                               # ref -> outcome
        self.pending = collections.OrderedDict()         # n -> action
        self.recent = collections.deque(maxlen=max(1, n_actions))
        self.step_tokens = {}            # action n -> read tokens after it
        self._last_action_n = None
        self.failures_list = []
        self.last_read = None
        self.freshness = []
        self.url_read = None
        self.url_actions = []
        self.url_by_session = {}
        self.since_events = []
        self.reads_by_doc = {}           # (doc, scope) -> last read
        self.max_v = 0

    def in_scope(self, ev):
        return self.scope is None or ev.get('session') == self.scope

    def feed(self, ev):
        k = ev.get('kind')
        n = ev.get('n')
        sess = ev.get('session')
        if k == 'session_start':
            self.sessions[sess] = {'start': ev, 'events': 0, 'end': None}
        info = self.sessions.setdefault(sess, {'start': None, 'events': 0,
                                               'end': None})
        info['events'] += 1
        if k == 'session_end':
            info['end'] = ev
        mine = self.in_scope(ev)
        if mine:
            self.kinds[k] += 1
        # --- file-wide derived views (section 5.3 / 5.5) ---
        if k == 'action':
            self.open_actions[n] = ev
            if _is_assertion(ev):
                self.pending[n] = ev
        elif k == 'action_outcome':
            ref = ev.get('ref')
            self.outcomes[ref] = ev
            self.open_actions.pop(ref, None)
            if ev.get('outcome', {}).get('status') == 'error':
                self.pending.pop(ref, None)
        elif k == 'invalidation':
            for d in ev.get('dropped', []):
                self.pending.pop(d.get('n'), None)
        elif k == 'note' and ev.get('label') == 'goal':
            self.goal_any = ev
            if mine:
                self.goal = ev
        elif k == 'freshness':
            self.freshness.append(ev)
            if ev.get('status') == 'dead':
                self.failures_list.append((ev, self.last_read))
        # --- scoped views ---
        if not mine:
            return
        if k == 'state_read':
            self.reads += 1
            tok = ev.get('tokens')
            if tok:
                self.tokens[tok.get('tokenizer', '?')] += tok.get('count', 0)
                if self._last_action_n is not None:
                    self.step_tokens[self._last_action_n] = \
                        self.step_tokens.get(self._last_action_n, 0) + \
                        tok.get('count', 0)
            if 'error' in ev:
                self.failures += 1
                self.failures_list.append((ev, self.last_read))
            else:
                self.prev_position = self.position
                self.position = ev
                if ev.get('url'):
                    self.url_by_session[sess] = ev.get('url')
            if self.url and ev.get('url') and _url_match(ev['url'], self.url):
                self.url_read = ev
            if self.since is not None and n > self.since:
                self.since_events.append(('read', ev, self._prior_read(ev)))
            if 'error' not in ev:
                self.reads_by_doc[(_doc_of(ev), ev.get('scope', 'page'))] = ev
            self.last_read = ev
        elif k == 'action':
            self.actions += 1
            self.recent.append(ev)
            self._last_action_n = n
            if self.url and (_url_match(ev.get('url', ''), self.url) or
                             _url_match(self.url_by_session.get(sess, ''),
                                        self.url)):
                self.url_actions.append(ev)
            if self.since is not None and n > self.since:
                self.since_events.append(('action', ev, None))
        elif k == 'action_outcome':
            if ev.get('outcome', {}).get('status') == 'error':
                self.failures += 1
                self.failures_list.append((ev, self.last_read))
                if self.since is not None and n > self.since:
                    self.since_events.append(('failure', ev, None))

    def _prior_read(self, ev):
        return self.reads_by_doc.get((_doc_of(ev), ev.get('scope', 'page')))


def _doc_of(ev):
    return ev.get('doc_key') or ev.get('url') or ''


def _url_match(a, b):
    if not a or not b:
        return False
    return a == b or a.rstrip('/') == b.rstrip('/')


def _changes(prev, cur):
    """Derived change detection between two same-scope reads of one
    document (section 5.2). Returns (flags, unavailable_reason)."""
    ph, ch = prev.get('hashes') or {}, cur.get('hashes') or {}
    if not ph or not ch:
        return None, 'no hashes on one of the reads'
    if ph.get('basis') != ch.get('basis'):
        return None, (f'hash bases differ ({ph.get("basis")} vs '
                      f'{ch.get("basis")}) - not comparable')
    flags = {}
    for m in ('state', 'structure', 'outline', 'weak'):
        if m in ph and m in ch:
            flags[m] = ph[m] != ch[m]
    return flags, None


def _change_word(flags):
    if flags.get('structure'):
        return 'meaningful change (structure)'
    if flags.get('outline'):
        return 'section-level change (outline)'
    if flags.get('weak'):
        return 'cosmetic churn (weak only)'
    if flags.get('state'):
        return 'state text changed'
    return 'unchanged'


def resolve_scope(rep_sessions, requested, caller):
    """Section 13.1 session default: the caller's session if known,
    else the most recent session in the file. A caller whose own
    session has no events in this file yet (a per-call client, or a
    fresh instance reading a predecessor) falls back to the most recent
    session that has - and the view says so."""
    if requested == '*':
        return None, 'all sessions'
    if requested:
        return requested, f'session {requested}'
    if caller and rep_sessions.get(caller, 0) > 1:
        return caller, f'session {caller} (yours)'
    with_events = [s for s, c in rep_sessions.items() if c > 1]
    pool = with_events or list(rep_sessions)
    if not pool:
        return None, 'all sessions'
    last = pool[-1]
    why = ('your session has no events yet; ' if caller else '')
    return last, f'session {last} ({why}most recent in file; session="*" for all)'


def build_view(path, mode='summary', caller=None, session=None, url=None,
               since=None, limit=None, live_probe=None, count_tokens=None,
               tokenizer='chars/4', writer_note=None):
    """Render one glazyr_trace response (body + footer) for a file."""
    count_tokens = count_tokens or (lambda s: max(1, len(s) // 4))
    sess_counts = collections.OrderedDict()

    def first(ev):
        s = ev.get('session')
        sess_counts[s] = sess_counts.get(s, 0) + 1
    rep = verify_file(path, on_event=first)
    if not rep['exists']:
        return f'Glazyr trace: no such file: {path}'
    if rep['empty']:
        return f'Glazyr trace: {os.path.basename(path)} is empty - no events yet.'

    scope, scope_label = resolve_scope(sess_counts, session, caller)
    n_actions = 10 if not limit else max(1, limit)
    model = Model(scope, mode, url=url, since=since, n_actions=n_actions)
    verify_file(path, on_event=model.feed, stop_after=rep['verified'])

    budgets = {'tail': 500, 'summary': 400, 'url': 500, 'since': 600,
               'failures': 1500, 'verify': 300}
    budget = budgets.get(mode, 500)
    renderer = {'summary': _v_summary, 'tail': _v_tail,
                'failures': _v_failures, 'url': _v_url,
                'since': _v_since, 'verify': _v_verify}.get(mode)
    if renderer is None:
        return (f'Glazyr trace: unknown mode {mode!r}. Modes: summary, tail, '
                f'failures, url, since, verify, note.')
    if mode == 'url' and not url:
        return 'Glazyr trace: url mode needs url=<the URL to report on>.'
    if mode == 'since' and since is None:
        return 'Glazyr trace: since mode needs since=<event index n>.'

    live = None
    if mode == 'tail' and live_probe is not None:
        try:
            live = live_probe()
        except Exception:
            live = None
        live = ('probe', live)

    # Section 13.4 cut order: hash values -> changed-flags, collapse
    # stub reads, token detail, then list truncation with "+N more".
    max_items = limit or 10
    steps = [dict(hash_values=True, collapse_stubs=False, token_detail=True,
                  max_items=max_items),
             dict(hash_values=False, collapse_stubs=False, token_detail=True,
                  max_items=max_items),
             dict(hash_values=False, collapse_stubs=True, token_detail=True,
                  max_items=max_items),
             dict(hash_values=False, collapse_stubs=True, token_detail=False,
                  max_items=max_items)]
    m = max_items
    while m > 1:
        m = max(1, m // 2)
        steps.append(dict(hash_values=False, collapse_stubs=True,
                          token_detail=False, max_items=m))
    text = None
    for opts in steps:
        body = renderer(model, rep, opts, live=live, scope_label=scope_label)
        text = body + '\n' + _footer(model, rep, path, body, count_tokens,
                                     tokenizer, mode, writer_note)
        if count_tokens(text) <= budget:
            break
    return text


def _footer(model, rep, path, body, count_tokens, tokenizer, mode,
            writer_note):
    lines = []
    lines.append(f'# trace: {os.path.basename(path)} · {rep["verified"]} '
                 f'verified events · chain: {chain_label(rep)}')
    lines.append(f'# head: {rep["head"] or "(no verified events)"}')
    if rep['chain'] == 'break':
        lines.append(f'# views are served from the verified prefix '
                     f'(n=1..{rep["break_at"] - 1}); {rep["break_reason"]}')
    elif rep['chain'] == 'torn tail':
        lines.append(f'# torn tail: line {rep["torn"]["line"]} is an '
                     f'incomplete write ({rep["torn"]["reason"]}) - a writer '
                     f'died mid-append; the verified prefix is the trace')
    if rep['warnings']:
        lines.append('# warning: ' + '; '.join(rep['warnings']))
    if rep['violations']:
        lines.append(f'# conformance: {len(rep["violations"])} violation(s), '
                     f'first: {rep["violations"][0]}')
    if rep['max_v'] > FORMAT_V:
        lines.append(f'# file version: v={rep["max_v"]} events present; this '
                     f'reader understands v={FORMAT_V} (newer kinds/fields '
                     f'ignored, not conformance-checked)')
    # Capability gaps that bear on the view (section 13.3).
    gaps = []
    for s, info in model.sessions.items():
        st = info.get('start')
        if model.scope is not None and s != model.scope:
            continue
        if st is None:
            continue
        kinds = set((st.get('capabilities') or {}).get('kinds', []))
        if 'freshness' not in kinds:
            gaps.append(f'freshness: not monitored by emitter '
                        f'{(st.get("emitter") or {}).get("name", "?")} '
                        f'(session {s})')
        if 'invalidation' not in kinds:
            gaps.append(f'invalidation: not emitted by session {s} - '
                        f'pending fills can never clear')
        cont = st.get('continues')
        if cont:
            lines.append(f'# continues: {cont.get("file")} @ '
                         f'{cont.get("head_hash")} (pass file= to follow)')
    for g in gaps[:3]:
        lines.append(f'# gap: {g}')
    if writer_note:
        lines.append(f'# {writer_note}')
    toks = sum(model.tokens.values())
    tz = ', '.join(sorted(model.tokens)) or tokenizer
    vol = (f'{model.reads} reads / {toks:,} tokens ({tz}) → this response'
           if mode in ('summary', 'tail', 'since', 'url') else None)
    # Own cost, self-inclusive: render once with a placeholder, count,
    # re-render with the count (digit width can shift it by a token).
    cost = 0
    for _ in range(3):
        cl = f'# cost: {cost} tokens ({tokenizer})' + \
             (f' · stands in for {vol}' if vol else '')
        txt = body + '\n' + '\n'.join(lines + [cl])
        new = count_tokens(txt)
        if new == cost:
            break
        cost = new
    lines.append(f'# cost: {cost} tokens ({tokenizer})' +
                 (f' · stands in for {vol}' if vol else ''))
    return '\n'.join(lines)


def _goal_lines(model):
    g = model.goal or model.goal_any
    if g is None:
        return ['goal: (none noted - glazyr_trace(mode="note", label="goal", '
                'text=...) records one)']
    tag = '' if g is model.goal else f' (session {g.get("session")})'
    return [f'goal (n={g["n"]}{tag}): {g.get("text", "")}']


def _interrupted_lines(model):
    if not model.open_actions:
        return ['interrupted actions: none']
    out = ['INTERRUPTED ACTIONS - begun, outcome unknown; non-idempotent '
           'effects (submits, posts, payments) may already have happened. '
           'Verify against the live page; do not blindly repeat:']
    for a in model.open_actions.values():
        out.append(f'  ! {_act_str(a)} (session {a.get("session")}, '
                   f'{a.get("ts")})')
    return out


def _position_lines(model, opts):
    p = model.position
    if p is None:
        return ['position: no successful state_read in scope']
    s = f'position (n={p["n"]}): {p.get("url", "(url unknown)")}'
    if p.get('title'):
        s += f' | {p["title"]}'
    bits = []
    if p.get('doc_key'):
        bits.append(f'doc_key={p["doc_key"]}')
    st = p.get('state') or {}
    if st:
        bits.append(f'state={st.get("epoch", "?")}/{st.get("seq", "?")}')
    h = p.get('hashes') or {}
    if h and opts['hash_values'] and h.get('state'):
        bits.append(f'state_hash={_short(h["state"])} ({h.get("basis")})')
    elif h and model.prev_position is not None and \
            _doc_of(model.prev_position) == _doc_of(p) and \
            model.prev_position.get('scope', 'page') == p.get('scope', 'page'):
        flags, why = _changes(model.prev_position, p)
        bits.append('vs previous read: ' + (why or _change_word(flags)))
    return [s] + (['  ' + ' · '.join(bits)] if bits else [])


def _live_lines(model, live):
    if live is None:
        return []
    probe = live[1]
    p = model.position
    st = (p or {}).get('state') or {}
    if probe is None:
        verdict = 'no_live_browser'
    elif not st.get('epoch'):
        return ['live_state: unavailable (trace position carries no state '
                'epoch to compare)']
    elif probe.get('epoch') != st.get('epoch'):
        verdict = 'producer_restarted'
    elif probe.get('seq') == st.get('seq'):
        verdict = 'matches'
    else:
        verdict = 'differs'
    hint = {'matches': 'browser survived, state unchanged - resume in place',
            'differs': 'browser survived, state moved on - peek before acting',
            'producer_restarted': 'browser did not survive - re-navigate and '
                                  'replay (verify pending fills)',
            'no_live_browser': 'no producer running - re-navigate and replay'}
    live_s = '' if probe is None else f' (live {probe.get("epoch")}/{probe.get("seq")})'
    return [f'live_state: {verdict}{live_s} - {hint[verdict]}']


def _pending_lines(model, opts):
    if not model.pending:
        return ['pending fills: none']
    items = list(model.pending.values())
    out = [f'pending fills ({len(items)}, unconfirmed assertions - verify '
           f'against the live page, never trust or blindly re-type):']
    cap = opts['max_items']
    for a in items[-cap:]:
        tag = ' [INTERRUPTED]' if a['n'] in model.open_actions else ''
        oc = model.outcomes.get(a['n'])
        prov = f' ({oc.get("provenance")})' if oc else ''
        out.append(f'  - {_act_str(a)}{prov}{tag}')
    if len(items) > cap:
        out.insert(1, f'  … +{len(items) - cap} earlier more')
    return out


def _recent_lines(model, opts):
    acts = list(model.recent)
    if not acts:
        return ['last actions: none in scope']
    cap = opts['max_items']
    shown = acts[-cap:]
    out = [f'last actions ({len(shown)} of {model.actions}):']
    if model.actions > len(shown):
        out.append(f'  … +{model.actions - len(shown)} more earlier')
    for a in shown:
        line = f'  {_act_str(a)} → {_outcome_str(model.outcomes.get(a["n"]))}'
        if opts['token_detail'] and a['n'] in model.step_tokens:
            line += f' · read {model.step_tokens[a["n"]]} tok'
        out.append(line)
    return out


def _v_tail(model, rep, opts, live=None, scope_label=''):
    out = [f'# glazyr_trace tail · {scope_label}']
    out += _goal_lines(model)
    out += _interrupted_lines(model)
    out += _position_lines(model, opts)
    out += _live_lines(model, live)
    out += _pending_lines(model, opts)
    out += _recent_lines(model, opts)
    return '\n'.join(out)


def _v_summary(model, rep, opts, live=None, scope_label=''):
    out = [f'# glazyr_trace summary · {scope_label}']
    out.append(f'sessions in file: {len(model.sessions)}')
    kinds = ', '.join(f'{k}={v}' for k, v in sorted(model.kinds.items()))
    out.append(f'events in scope by kind: {kinds or "none"}')
    tok = ', '.join(f'{v:,} ({k})' for k, v in sorted(model.tokens.items()))
    out.append(f'reads={model.reads} actions={model.actions} '
               f'failures={model.failures} tokens={tok or "0"}')
    out += _goal_lines(model)
    if model.open_actions:
        out.append(f'interrupted actions: {len(model.open_actions)} - '
                   + ', '.join(f'n={n} {a.get("action")}'
                               for n, a in model.open_actions.items())
                   + ' (mode="tail" for detail)')
    out += _position_lines(model, opts)
    return '\n'.join(out)


def _v_failures(model, rep, opts, live=None, scope_label=''):
    out = [f'# glazyr_trace failures · {scope_label}']
    items = model.failures_list
    if not items:
        out.append('no failures in the verified prefix')
        return '\n'.join(out)
    cap = max(opts['max_items'], 50) if opts['max_items'] else 50
    shown = items[-cap:]
    if len(items) > len(shown):
        out.append(f'… +{len(items) - len(shown)} more earlier')
    for ev, before in shown:
        k = ev.get('kind')
        if k == 'action_outcome':
            txt = (f'n={ev["n"]} action n={ev.get("ref")} failed '
                   f'({ev.get("provenance")}): '
                   f'{ev.get("outcome", {}).get("error", "")}')
        elif k == 'state_read':
            txt = (f'n={ev["n"]} read failed ({ev.get("provenance")}): '
                   f'{ev.get("error", "")}')
        else:
            txt = (f'n={ev["n"]} freshness dead ({ev.get("provenance")}): '
                   f'{ev.get("detail", "")}')
        out.append(txt)
        if before is not None:
            out.append(f'    read before: n={before["n"]} '
                       f'{before.get("url", "(url unknown)")}'
                       + (f' [{before.get("scope")}]' if before.get('scope') else ''))
    return '\n'.join(out)


def _v_url(model, rep, opts, live=None, scope_label=''):
    out = [f'# glazyr_trace url {model.url} · {scope_label}']
    r = model.url_read
    if r is None and not model.url_actions:
        out.append('not visited in scope (no state_read or action on this URL)')
        return '\n'.join(out)
    if r is not None:
        s = f'latest read: n={r["n"]} {r.get("title", "")}'
        if r.get('doc_key'):
            s += f' doc_key={r["doc_key"]}'
        if opts['token_detail'] and r.get('tokens'):
            s += f' · {r["tokens"]["count"]} tok'
        h = r.get('hashes') or {}
        if opts['hash_values'] and h.get('state'):
            s += f' · state_hash={_short(h["state"])}'
        out.append(s)
    acts = model.url_actions
    cap = opts['max_items']
    out.append(f'actions on it ({len(acts)}):')
    if len(acts) > cap:
        out.append(f'  … +{len(acts) - cap} more earlier')
    for a in acts[-cap:]:
        out.append(f'  {_act_str(a)} → {_outcome_str(model.outcomes.get(a["n"]))}')
    return '\n'.join(out)


def _v_since(model, rep, opts, live=None, scope_label=''):
    out = [f'# glazyr_trace since n={model.since} · {scope_label}']
    evs = model.since_events
    if not evs:
        out.append(f'nothing in scope after n={model.since}')
        return '\n'.join(out)
    docs = []
    for kind, ev, prior in evs:
        if kind == 'read' and 'error' not in ev:
            d = ev.get('url') or ev.get('doc_key')
            if d and d not in docs:
                docs.append(d)
    out.append(f'documents visited: {len(docs)}' +
               (' - ' + ', '.join(docs[:opts['max_items']]) if docs else ''))
    if len(docs) > opts['max_items']:
        out.append(f'  … +{len(docs) - opts["max_items"]} more')
    lines = []
    last_doc = None
    for kind, ev, prior in evs:
        if kind == 'read':
            if 'error' in ev:
                continue
            if ev.get('doc_key') and last_doc and ev['doc_key'] != last_doc:
                lines.append(f'  n={ev["n"]} doc_key change → {ev["doc_key"]} '
                             f'(observed navigation)')
            if ev.get('doc_key'):
                last_doc = ev['doc_key']
            if ev.get('stub') and opts['collapse_stubs']:
                continue
            if prior is not None and prior['n'] != ev['n']:
                flags, why = _changes(prior, ev)
                if why:
                    lines.append(f'  n={ev["n"]} vs n={prior["n"]} '
                                 f'[{ev.get("scope", "page")}]: change '
                                 f'detection unavailable - {why}')
                else:
                    word = _change_word(flags)
                    s = (f'  n={ev["n"]} vs n={prior["n"]} '
                         f'[{ev.get("scope", "page")}]: {word}')
                    if opts['hash_values']:
                        h = ev.get('hashes') or {}
                        s += (f' (structure {_short(h.get("structure"))}, '
                              f'outline {_short(h.get("outline"))})')
                    lines.append(s)
        elif kind == 'action':
            lines.append(f'  {_act_str(ev)} → '
                         f'{_outcome_str(model.outcomes.get(ev["n"]))}')
        elif kind == 'failure':
            lines.append(f'  n={ev["n"]} FAILURE action n={ev.get("ref")}: '
                         f'{ev.get("outcome", {}).get("error", "")}')
    cap = opts['max_items'] * 2
    if len(lines) > cap:
        keep = [l for l in lines if 'FAILURE' in l]
        rest = [l for l in lines if 'FAILURE' not in l][-max(0, cap - len(keep)):]
        dropped = len(lines) - len(keep) - len(rest)
        lines = ([f'  … +{dropped} more earlier'] if dropped else []) + \
            sorted(keep + rest, key=lambda l: int(re.search(r'n=(\d+)', l).group(1)))
    out += lines
    return '\n'.join(out)


def _v_verify(model, rep, opts, live=None, scope_label=''):
    out = ['# glazyr_trace verify']
    out.append(f'chain: {chain_label(rep)}')
    out.append(f'lines read: {rep["lines"]} · events verified: {rep["verified"]}')
    if rep['chain'] == 'break':
        out.append(f'first break: n={rep["break_at"]} - {rep["break_reason"]} '
                   f'(blaming n={rep["break_at"]} is a reporting convention, '
                   f'not a diagnosis)')
    if rep['torn']:
        out.append(f'torn tail at line {rep["torn"]["line"]}: '
                   f'{rep["torn"]["reason"]}')
    out.append(f'head of verified prefix: {rep["head"] or "(empty)"}')
    for v in rep['violations'][:10]:
        out.append(f'conformance: {v}')
    if len(rep['violations']) > 10:
        out.append(f'… +{len(rep["violations"]) - 10} more violations')
    for w in rep['warnings']:
        out.append(f'warning: {w}')
    out.append('accident detection only: anyone who can edit the file can '
               'recompute the chain - this is not tamper-evidence')
    return '\n'.join(out)
