#!/usr/bin/env python3
"""Glazyr filter v0.1 - reader-side curation over AnnotatedPageContent protos.

v0.1 changes vs v0: content merging (one line per paragraph/list-item/cell
with anchors folded in), DOM ids only on actionable lines, URL diet
(origin-stripping + citation-fragment compression), honest node accounting
(total counted by an independent walk).

Levels: none | light | standard | aggressive
"""
import sys, collections, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gen'))
from components.optimization_guide.proto.features import common_quality_data_pb2 as cq

TYPE = cq.ContentAttributeType
T = {v.number: v.name.replace('CONTENT_ATTRIBUTE_', '') for v in TYPE.DESCRIPTOR.values}

TEXT_CAP = {'none': None, 'light': 1000, 'standard': 512, 'aggressive': 120}
MERGE_TYPES = {'PARAGRAPH', 'LIST_ITEM', 'TABLE_CELL', 'HEADING'}
# v0.2.2 sibling collapse: a long run of same-shape siblings (reference
# lists, table rows, release lists) is mostly repetition. Render the
# first few, fold the rest behind one line with an expansion handle.
COLLAPSE_TYPES = {'LIST_ITEM', 'TABLE_ROW', 'TABLE_CELL'}
COLLAPSE_MIN_RUN = 12   # runs longer than this collapse
COLLAPSE_KEEP = 8       # leading siblings rendered before the fold

# v0.3.4: credential-bearing form controls (password inputs and their
# kin by field name). Their values are never echoed - not the bridge's
# typed-value overlay, not the producer's own field_value, not as a
# find match (a substring oracle is an echo too).
import re as _re
_CRED_NAME = _re.compile(
    r'passw|passwd|passcode|passphrase|pwd|secret|otp|one.?time|totp|'
    r'cvv|cvc|csc|security.?code|card.?num|cc.?num', _re.I)

def is_credential_control(fc):
    """fc: a FormControlData message."""
    if 'PASSWORD' in cq.FormControlType.Name(fc.form_control_type):
        return True
    return bool(_CRED_NAME.search(fc.field_name or ''))

def redact_url(url):
    """A GET-submitted credential lands in the URL (?password=...):
    credential-named query parameters keep their name, lose their value."""
    if not url or '?' not in url:
        return url
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, quote
    try:
        p = urlsplit(url)
        pairs = parse_qsl(p.query, keep_blank_values=True)
    except Exception:
        return url
    if not any(_CRED_NAME.search(k) for k, _ in pairs):
        return url
    q = '&'.join(f'{quote(k, safe="")}=' + ('REDACTED' if _CRED_NAME.search(k)
                                            else quote(v, safe=''))
                 for k, v in pairs)
    return urlunsplit((p.scheme, p.netloc, p.path, q, p.fragment))

def node_text(node):
    a = node.content_attributes
    if a.WhichOneof('content_data') == 'text_data':
        return a.text_data.text_content
    return ''

def count_nodes(node):
    return 1 + sum(count_nodes(c) for c in node.children_nodes)

def subtree_has(node, types):
    if T.get(node.content_attributes.attribute_type) in types:
        return True
    return any(subtree_has(c, types) for c in node.children_nodes)

def descendant_count(node, kind):
    n = 0
    for c in node.children_nodes:
        if T.get(c.content_attributes.attribute_type) == kind:
            n += 1
        n += descendant_count(c, kind)
    return n

class Filter:
    def __init__(self, apc, level, overlay=None):
        self.level = level
        self.cap = TEXT_CAP[level]
        self.stats = collections.Counter()
        self.lines = []
        # overlay: dom_id -> list/dict of bridge-asserted annotations
        # (typed values, CHECKED) merged into form-control lines.
        self.overlay = overlay or {}
        self._seen_hrefs = set()  # in-text href dedup (first occurrence pays)
        origin = ''
        try:
            origin = apc.main_frame_data.security_origin.value
        except Exception:
            pass
        self.origin = origin.rstrip('/')

    def diet_url(self, url, maxlen=90):
        if self.level == 'none':
            return url
        if self.origin and url.startswith(self.origin):
            url = url[len(self.origin):] or '/'
        if self.level in ('standard', 'aggressive'):
            if '#cite' in url:
                url = url.split('#')[0] + '#cite'
            if len(url) > maxlen:
                url = url[:maxlen] + '…'
        return url

    def fmt(self, s, cap=None):
        s = ' '.join(s.split())
        cap = self.cap if cap is None else cap
        if cap and len(s) > cap:
            # Head+tail: keep the end of long text too. Tails carry the
            # payload in JSON responses and the point of long sentences.
            self.stats['truncated'] += 1
            head = max(1, int(cap * 0.6))
            tail = max(1, cap - head)
            return s[:head] + ' … ' + s[-tail:]
        return s

    def merged_text(self, node):
        """Text of a subtree in document order. Anchors fold in with their
        DOM id (so they stay clickable from the merged view); the URL rides
        along at none/light levels."""
        parts = []
        def rec(n):
            a = n.content_attributes
            t = T.get(a.attribute_type)
            if t == 'ANCHOR':
                txt = ' '.join(node_text(d) for d in self._texts(n)).strip()
                dom = a.common_ancestor_dom_node_id
                idref = f' [{dom}]' if dom else ''
                if self.level in ('none', 'light'):
                    parts.append(f'{txt}{idref} <{self.diet_url(a.anchor_data.url)}>' if txt
                                 else f'<{self.diet_url(a.anchor_data.url)}>{idref}')
                else:
                    # v0.2: in-text anchors carry their (dieted) href -
                    # dogfood agents clicked blind without it. First
                    # occurrence of a target pays for the href; repeats
                    # render bare (same destination, still clickable).
                    href = self.diet_url(a.anchor_data.url, 40)
                    if href in self._seen_hrefs:
                        parts.append(f'{txt}{idref}' if txt else f'[link]{idref}')
                    else:
                        self._seen_hrefs.add(href)
                        parts.append(f'{txt}{idref} -> {href}' if txt
                                     else f'[link]{idref} -> {href}')
                return
            txt = node_text(n)
            if txt.strip():
                parts.append(txt.strip())
            for c in n.children_nodes:
                if c is not n:
                    rec(c)
        rec(node)
        return ' '.join(parts)

    def _texts(self, node):
        out = []
        def rec(n):
            if node_text(n):
                out.append(n)
            for c in n.children_nodes:
                rec(c)
        rec(node)
        return out

    def _collapsible(self, node):
        t = T.get(node.content_attributes.attribute_type)
        return t in COLLAPSE_TYPES and not subtree_has(node, {'FORM_CONTROL'})

    def rec_children(self, node, depth):
        """Recurse into children, folding a short TEXT sibling into the
        FORM_CONTROL it labels. v0.2.3 label association: a label that
        immediately PRECEDES its control wins (the v0.2 rule used only
        the following text, which shifted every label by one field on
        label-first forms - the Hot and Pressy calculator); the
        following text is still used when nothing precedes, which is
        the radio/checkbox convention. Long runs of same-type siblings
        collapse past COLLAPSE_KEEP (v0.2.2)."""
        kids = list(node.children_nodes)
        parent_dom = node.content_attributes.common_ancestor_dom_node_id
        pending_label = None
        i = 0
        while i < len(kids):
            c = kids[i]
            ct_i = T.get(c.content_attributes.attribute_type)
            if ct_i == 'TEXT' and i + 1 < len(kids) \
                    and T.get(kids[i + 1].content_attributes.attribute_type) == 'FORM_CONTROL':
                lt = ' '.join(node_text(c).split())
                if 0 < len(lt) <= 40:
                    pending_label = lt
                    i += 1  # held as the next control's label
                    continue
            if self.level != 'none' and self._collapsible(c):
                ct = T.get(c.content_attributes.attribute_type)
                j = i
                while j < len(kids) and self._collapsible(kids[j]) \
                        and T.get(kids[j].content_attributes.attribute_type) == ct:
                    j += 1
                run = j - i
                if run > COLLAPSE_MIN_RUN:
                    for k in range(i, i + COLLAPSE_KEEP):
                        self.rec(kids[k], depth)
                    skipped = run - COLLAPSE_KEEP
                    self.stats['collapsed'] += skipped
                    tag = ct.lower().replace('_', '-')
                    handle = (f' - peek_subtree [{parent_dom}] to expand'
                              if parent_dom else '')
                    self.lines.append(
                        f'{"  " * depth}… +{skipped} similar {tag} '
                        f'collapsed{handle}')
                    self.stats['shown'] += 1
                    i = j
                    continue
            hint = None
            if ct_i == 'FORM_CONTROL':
                if pending_label:
                    hint = pending_label
                    pending_label = None
                elif i + 1 < len(kids):
                    nxt = kids[i + 1]
                    if T.get(nxt.content_attributes.attribute_type) == 'TEXT':
                        lt = ' '.join(node_text(nxt).split())
                        if 0 < len(lt) <= 40:
                            hint = lt
                            i += 1  # consumed as the label
            self.rec(c, depth, hint)
            i += 1

    def rec(self, node, depth, label_hint=None):
        a = node.content_attributes
        t = T.get(a.attribute_type, 'UNKNOWN')
        ii = a.interaction_info
        dom = a.common_ancestor_dom_node_id
        ind = '  ' * depth
        level = self.level

        # --- elisions ---
        if level != 'none':
            if t in ('SVG_ROOT', 'CANVAS'):
                self.stats['elided_svg_canvas'] += count_nodes(node)
                return
            if t == 'TEXT' and not node_text(node).strip():
                self.stats['elided_ws_text'] += 1
                return
            if level in ('standard', 'aggressive') and t == 'IMAGE' \
                    and not a.image_data.image_caption and not a.label:
                self.stats['elided_bare_image'] += 1
                return
            if level in ('standard', 'aggressive') and t == 'CONTAINER' \
                    and not a.label:
                self.stats['flattened_container'] += 1
                self.rec_children(node, depth)
                return
            if level == 'aggressive' and t == 'TEXT':
                g = a.geometry
                if not (g.HasField('visible_bounding_box')
                        and g.visible_bounding_box.width > 0):
                    self.stats['elided_offscreen_text'] += 1
                    return

        # --- actionable ---
        if t == 'ANCHOR':
            txt = self.fmt(' '.join(node_text(d) for d in self._texts(node)), 120)
            dis = ' DISABLED' if ii.is_disabled else ''
            self.lines.append(f'{ind}[{dom}] link {txt!r} -> {self.diet_url(a.anchor_data.url)}{dis}')
            self.stats['shown'] += 1
            return
        if t == 'FORM_CONTROL':
            fc = a.form_control_data
            fct = cq.FormControlType.Name(fc.form_control_type).replace('FORM_CONTROL_TYPE_', '')
            extra = f' name={fc.field_name!r}'
            if fc.field_value:
                if is_credential_control(fc):
                    extra += ' val=<redacted>'
                else:
                    extra += f' val={fc.field_value[:30]!r}'
            if fc.select_options:
                extra += f' options={len(fc.select_options)}'
                # v0.2.3: name the options (first few) - a bare count
                # gave agents nothing to choose between or click.
                try:
                    opts = []
                    for o in fc.select_options[:6]:
                        ot = o if isinstance(o, str) else (
                            getattr(o, 'text', '') or getattr(o, 'value', ''))
                        if ot:
                            opts.append(str(ot)[:24])
                    if opts:
                        extra += ': ' + ' | '.join(opts)
                except Exception:
                    pass
            if fc.is_checked:
                extra += ' CHECKED'
            if fc.placeholder:
                extra += f' ph={fc.placeholder[:30]!r}'
            if label_hint:
                extra += f' label={label_hint!r}'
            if dom in self.overlay:
                ann = self.overlay[dom]
                parts = ann.values() if isinstance(ann, dict) else ann
                extra += ' ' + ' '.join(parts)
            self.lines.append(f'{ind}[{dom}] input {fct}{extra}')
            self.stats['shown'] += 1
            self.rec_children(node, depth + 1)
            return

        # --- merged content ---
        if t in MERGE_TYPES and not subtree_has(node, {'FORM_CONTROL'}):
            txt = self.fmt(self.merged_text(node))
            if txt:
                tag = 'heading' if t == 'HEADING' else t.lower().replace('_', '-')
                n_sub = count_nodes(node)
                big = n_sub > 120
                idref = f'[{dom}] ' if (dom and (big or ii.is_clickable or ii.is_focusable)) else ''
                suffix = f' [+{n_sub} nodes - peek_subtree to expand]' if big else ''
                self.lines.append(f'{ind}{idref}{tag} {txt!r}{suffix}')
                self.stats['shown'] += 1
                self.stats['merged'] += 1
            else:
                self.stats['elided_empty'] += 1
            return
        if t == 'TEXT':
            txt = self.fmt(node_text(node))
            if txt:
                self.lines.append(f'{ind}text {txt!r}')
                self.stats['shown'] += 1
            return
        if t == 'IMAGE':
            cap_txt = a.image_data.image_caption or a.label or ''
            self.lines.append(f'{ind}image {cap_txt[:60]!r}')
            self.stats['shown'] += 1
            return
        if t == 'FORM':
            self.lines.append(f'{ind}form action={self.diet_url(a.form_data.action_url)!r}')
            self.stats['shown'] += 1
            self.rec_children(node, depth + 1)
            return
        # structural / other
        lbl = f' {a.label[:40]!r}' if a.label else ''
        idref = f'[{dom}] ' if (ii.is_clickable or ii.is_focusable) else ''
        self.lines.append(f'{ind}{idref}{t.lower()}{lbl}')
        self.stats['shown'] += 1
        self.rec_children(node, depth + 1)

    def run(self, apc):
        total = count_nodes(apc.root_node)
        self.rec(apc.root_node, 0)
        s = self.stats
        elided = sum(v for k, v in s.items() if k.startswith('elided'))
        detail = ' '.join(f'{k[7:]}={v}' for k, v in sorted(s.items())
                          if k.startswith('elided'))
        header = (f'# total={total} shown={s["shown"]} merged={s["merged"]} '
                  f'flattened={s["flattened_container"]} truncated={s["truncated"]} '
                  + (f'collapsed={s["collapsed"]} ' if s["collapsed"] else '')
                  + f'elided={elided}' + (f' ({detail})' if detail else ''))
        return header + '\n' + '\n'.join(self.lines), dict(s, total=total)

def load(path):
    apc = cq.AnnotatedPageContent()
    apc.ParseFromString(open(path, 'rb').read())
    return apc

def serialize(apc, level='standard', overlay=None):
    return Filter(apc, level, overlay).run(apc)

def serialize_subtree(apc, node, level='standard', overlay=None):
    """Serialize one node's subtree only - the targeted recovery path
    instead of a whole-page raw read."""
    f = Filter(apc, level, overlay)
    total = count_nodes(node)
    f.rec(node, 0)
    s = f.stats
    dom = node.content_attributes.common_ancestor_dom_node_id
    header = (f'# subtree of [{dom}] nodes={total} shown={s["shown"]} '
              f'truncated={s["truncated"]}')
    return header + '\n' + '\n'.join(f.lines), dict(s, total=total)

def find_nodes(apc, query, limit=20):
    """Reader-side find: nodes whose text/label/url/field name or value
    contains the query (case-insensitive). Returns ContentNodes.

    v0.2.3: merged-content nodes (headings, paragraphs, list items,
    table cells) carry their text in child TEXT nodes, so their own
    text is empty and heading text used to be unfindable ("Example
    Domain" on example.com matched 0). Their haystack now includes
    the subtree's text."""
    q = query.lower()
    out = []
    def subtree_text(n, cap=400):
        parts = []
        def rec(m):
            if len(parts) >= 48:
                return
            txt = node_text(m)
            if txt.strip():
                parts.append(txt.strip())
            for c in m.children_nodes:
                rec(c)
        rec(n)
        return ' '.join(parts)[:cap]
    def haystack(n):
        a = n.content_attributes
        parts = [node_text(n), a.label or '']
        t = T.get(a.attribute_type)
        if t == 'ANCHOR':
            parts.append(a.anchor_data.url)
        elif t == 'FORM_CONTROL':
            parts.append(a.form_control_data.field_name)
            if not is_credential_control(a.form_control_data):
                parts.append(a.form_control_data.field_value)
        # Merged-content nodes carry their text in children; so do
        # some heading-like wrappers the producer types otherwise
        # (example.com's H1 is not a HEADING node). Any text-less
        # parent gets its subtree text as haystack.
        if t in MERGE_TYPES or (not node_text(n).strip() and n.children_nodes):
            parts.append(subtree_text(n))
        return ' '.join(parts).lower()
    def rec(n):
        if len(out) >= limit:
            return
        if n.content_attributes.common_ancestor_dom_node_id and q in haystack(n):
            out.append(n)
        for c in n.children_nodes:
            rec(c)
    rec(apc.root_node)
    return out

def render_node(apc, node, level='standard', overlay=None, max_lines=3):
    """Serialize a single node (plus a couple of lines of context) the
    same way the full view would - used by find results."""
    f = Filter(apc, level, overlay)
    f.rec(node, 0)
    lines = f.lines[:max_lines]
    if len(f.lines) > max_lines:
        lines.append('…')
    return '\n'.join(lines)

if __name__ == '__main__':
    import tiktoken
    enc = tiktoken.get_encoding('cl100k_base')
    for name in ['example', 'form', 'wiki']:
        apc = load(f'payloads/{name}.bin')
        print(f'== {name}')
        for level in ['none', 'light', 'standard', 'aggressive']:
            text, st = serialize(apc, level)
            print(f'   {level:10s} tokens={len(enc.encode(text)):7d} shown={st["shown"]}/{st["total"]}')
