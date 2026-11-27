#!/usr/bin/env python3
"""Glazyr Trace unit tests (v0.3.4). Stdlib unittest; run with the
bridge venv python from the repo root:

    python -m unittest discover -s tests -v

The reference semantics are trace-fixtures/check_fixtures.py: its own
check() function is loaded and used as the judge for writer output.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import types
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(REPO, 'trace-fixtures')
sys.path.insert(0, os.path.join(REPO, 'bridge'))

import glazyr_trace as gt  # noqa: E402


def _load_reference_check():
    """check() from check_fixtures.py, without running its module-level
    fixture loop (everything from `EXPECT = {` on)."""
    src = open(os.path.join(FIX, 'check_fixtures.py'), encoding='utf-8').read()
    src = src[:src.index('EXPECT = {')]
    ns = {'__file__': os.path.join(FIX, 'check_fixtures.py')}
    exec(compile(src, 'check_fixtures.py', 'exec'), ns)
    return ns['check']


REF_CHECK = _load_reference_check()

# Expected verdicts from trace-fixtures/README.md, in this reader's terms
# (a duplicate key is "break at n=3" + a non-conformance, per section 6).
EXPECT = {
    'valid-basic.trace.jsonl': ('ok', 12, []),
    'valid-swivel.trace.jsonl': ('ok', 8, []),
    'valid-interrupted.trace.jsonl': ('ok', 6, [6]),
    'valid-incomparable-hashes.trace.jsonl': ('ok', 3, []),
    'invalid-torn-tail.trace.jsonl': ('torn tail', 5, None),
    'invalid-broken-chain.trace.jsonl': ('break at n=5', 4, None),
    'invalid-null-placeholder.trace.jsonl': ('ok', 3, None),
    'invalid-duplicate-key.trace.jsonl': ('break at n=3', 2, None),
}


def interrupted(path):
    m = gt.Model(None, 'tail')
    gt.verify_file(path, on_event=m.feed)
    return list(m.open_actions)


class FixtureVerdicts(unittest.TestCase):
    def test_all_fixtures(self):
        for name, (chain, verified, intr) in EXPECT.items():
            with self.subTest(name):
                p = os.path.join(FIX, name)
                rep = gt.verify_file(p)
                self.assertEqual(gt.chain_label(rep), chain)
                self.assertEqual(rep['verified'], verified)
                if intr is not None:
                    self.assertEqual(interrupted(p), intr)

    def test_null_placeholder_violation(self):
        rep = gt.verify_file(os.path.join(FIX, 'invalid-null-placeholder.trace.jsonl'))
        self.assertTrue(any(v.startswith('n=3: null value at doc_key')
                            for v in rep['violations']), rep['violations'])

    def test_duplicate_key_violation(self):
        rep = gt.verify_file(os.path.join(FIX, 'invalid-duplicate-key.trace.jsonl'))
        self.assertTrue(any('duplicate key' in v for v in rep['violations']))

    def test_tail_names_interrupted(self):
        out = gt.build_view(os.path.join(FIX, 'valid-interrupted.trace.jsonl'),
                            mode='tail')
        self.assertIn('INTERRUPTED ACTIONS', out)
        self.assertIn('n=6 click', out)
        self.assertIn('chain: ok', out)
        self.assertIn('# head: ', out)
        # Goal note and interrupted report precede everything else.
        self.assertLess(out.index('goal'), out.index('INTERRUPTED'))
        self.assertLess(out.index('INTERRUPTED'), out.index('position'))

    def test_incomparable_bases_unavailable(self):
        out = gt.build_view(os.path.join(FIX, 'valid-incomparable-hashes.trace.jsonl'),
                            mode='since', since=0)
        self.assertIn('unavailable', out)
        self.assertNotIn('unchanged', out.split('# trace:')[0])

    def test_basic_cosmetic_and_pending(self):
        p = os.path.join(FIX, 'valid-basic.trace.jsonl')
        out = gt.build_view(p, mode='since', since=0)
        self.assertIn('cosmetic churn', out)
        m = gt.Model(None, 'tail')
        gt.verify_file(p, on_event=m.feed)
        self.assertEqual(list(m.pending), [])

    def test_broken_chain_footer(self):
        out = gt.build_view(os.path.join(FIX, 'invalid-broken-chain.trace.jsonl'),
                            mode='verify')
        self.assertIn('break at n=5', out)
        self.assertIn('verified prefix (n=1..4)', out)

    def test_torn_tail_footer(self):
        out = gt.build_view(os.path.join(FIX, 'invalid-torn-tail.trace.jsonl'))
        self.assertIn('chain: torn tail', out)

    def test_missing_and_empty(self):
        d = tempfile.mkdtemp()
        try:
            self.assertIn('no such file', gt.build_view(os.path.join(d, 'x.trace.jsonl')))
            open(os.path.join(d, 'e.trace.jsonl'), 'w').close()
            self.assertIn('empty', gt.build_view(os.path.join(d, 'e.trace.jsonl')))
        finally:
            shutil.rmtree(d)


def _writer(d, instance='glazyr-999999'):
    return gt.TraceWriter(d, instance, {'name': 'glazyr-bridge', 'version': 't'},
                          {'kinds': ['session_start', 'state_read', 'action',
                                     'action_outcome', 'note', 'session_end'],
                           'fields': []}, tokenizer='cl100k')


class Writer(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_chain_judged_by_reference_checker(self):
        w = _writer(self.d)
        a = w.append('s1', 'action', 'asserted',
                     {'action': 'type', 'cause': 'agent', 'value': 'hé\ud800llo',
                      'target': {'desc': 'x'}, 'skip': None, 'f': 1.6})
        w.append('s1', 'action_outcome', 'asserted', {'ref': a, 'outcome': {'status': 'ok'}})
        w.append('s1', 'state_read', 'observed',
                 {'url': 'https://e.example/', 'tokens': {'count': 7, 'tokenizer': 'cl100k'}})
        w.session_end('s1', 'agent_exit')
        v = REF_CHECK(w.path)
        self.assertEqual(v['chain'], 'ok')
        self.assertEqual(v['verified'], 5)
        self.assertEqual(v['interrupted'], [])
        self.assertNotIn('violations', v)
        rep = gt.verify_file(w.path)
        self.assertEqual(rep['chain'], 'ok')
        self.assertEqual(rep['violations'], [])
        self.assertEqual(rep['head'], w.head)
        lines = open(w.path, encoding='utf-8').read().splitlines()
        ev = json.loads(lines[1])
        self.assertEqual(ev['value'], 'hé�llo')     # lone surrogate replaced
        self.assertNotIn('skip', ev)                      # absent, never null
        self.assertEqual(ev['f'], 2)                      # integers only
        self.assertEqual(json.loads(lines[0])['kind'], 'session_start')
        end = json.loads(lines[-1])
        self.assertEqual(end['totals'], {'reads': 1, 'actions': 1, 'failures': 0,
                                         'tokens': {'count': 7, 'tokenizer': 'cl100k'}})

    def test_concurrent_appends_share_one_lock(self):
        w = _writer(self.d)

        def burst(s):
            for i in range(150):
                w.append(s, 'note', 'asserted', {'cause': 'agent', 'text': f'{s}-{i}'})
        ts = [threading.Thread(target=burst, args=(f's{k}',)) for k in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        rep = gt.verify_file(w.path)
        self.assertEqual(rep['chain'], 'ok')
        self.assertEqual(rep['verified'], 6 * 150 + 6)    # + 6 session_starts
        self.assertEqual(rep['violations'], [])
        self.assertEqual(REF_CHECK(w.path)['chain'], 'ok')

    def test_torn_append_then_new_writer_continues(self):
        w1 = _writer(self.d)
        w1.append('s1', 'action', 'asserted',
                  {'action': 'click', 'cause': 'agent', 'target': {'desc': 'Pay'}})
        with open(w1.path, 'ab') as f:                    # writer dies mid-append
            f.write(b'{"v":1,"n":3,"ts":"2026')
        rep = gt.verify_file(w1.path)
        self.assertEqual(rep['chain'], 'torn tail')
        self.assertEqual(rep['verified'], 2)
        self.assertEqual(REF_CHECK(w1.path)['chain'], 'torn tail')
        self.assertEqual(interrupted(w1.path), [2])
        os.utime(w1.path, (1, 1))
        w2 = _writer(self.d, instance='glazyr-999998')
        w2.append('s2', 'note', 'asserted', {'cause': 'agent', 'text': 'resume'})
        self.assertNotEqual(w2.path, w1.path)
        first = json.loads(open(w2.path, encoding='utf-8').readline())
        self.assertEqual(first['n'], 1)
        self.assertEqual(first['prev_hash'], gt.GENESIS)
        self.assertEqual(first['continues'], {'file': w1.path, 'head_hash': rep['head']})
        # The dead file was never appended to by the new writer.
        self.assertEqual(gt.verify_file(w1.path)['verified'], 2)
        tail = gt.build_view(w1.path, mode='tail')
        self.assertIn('n=2 click Pay', tail)
        self.assertIn('INTERRUPTED', tail)
        self.assertIn('torn tail', tail)

    def test_tamper_detected(self):
        w = _writer(self.d)
        for i in range(5):
            w.append('s', 'note', 'asserted', {'cause': 'agent', 'text': f't{i}'})
        lines = open(w.path, encoding='utf-8').read().splitlines(True)
        lines[2] = lines[2].replace('t1', 'tX')
        open(w.path, 'w', encoding='utf-8').writelines(lines)
        rep = gt.verify_file(w.path)
        self.assertEqual(gt.chain_label(rep), 'break at n=3')
        self.assertEqual(rep['verified'], 2)
        self.assertEqual(REF_CHECK(w.path)['chain'], 'break at n=3')

    def test_crlf_survives(self):
        w = _writer(self.d)
        w.append('s', 'note', 'asserted', {'cause': 'agent', 'text': 'a'})
        data = open(w.path, 'rb').read().replace(b'\n', b'\r\n')
        open(w.path, 'wb').write(data)
        self.assertEqual(gt.verify_file(w.path)['chain'], 'ok')

    def test_tail_budget_keeps_never_cut_items(self):
        w = _writer(self.d)
        w.append('s', 'note', 'asserted', {'cause': 'agent', 'label': 'goal',
                                           'text': 'Buy the thing'})
        for i in range(40):
            n = w.append('s', 'action', 'asserted', {
                'action': 'type', 'cause': 'agent', 'value': f'v{i}',
                'target': {'desc': f'[{i}] input', 'sig': {'scheme': 'glazyr-sig',
                                                          'type': 'FORM_CONTROL',
                                                          'name': f'f{i}'}}})
            w.append('s', 'action_outcome', 'asserted', {'ref': n, 'outcome': {'status': 'ok'}})
            w.append('s', 'state_read', 'observed', {
                'url': 'https://x.example/', 'tokens': {'count': 900, 'tokenizer': 'cl100k'},
                'hashes': {'basis': 'b', 'state': 'a' * 64}})
        w.append('s', 'action', 'asserted', {'action': 'click', 'cause': 'agent',
                                             'target': {'desc': 'Submit order',
                                                        'sig': {'scheme': 'glazyr-sig',
                                                                'type': 'FORM_CONTROL',
                                                                'fctype': 'BUTTON_SUBMIT'}}})
        out = gt.build_view(w.path, mode='tail', count_tokens=lambda s: len(s) // 4)
        body = out.split('# trace:')[0]
        self.assertIn('Buy the thing', out)
        self.assertIn('Submit order', out)
        self.assertIn('"fctype":"BUTTON_SUBMIT"', out)    # signature never cut
        self.assertIn('more', body)                       # list truncation marker
        self.assertLessEqual(len(out) // 4, 500)


class BridgeFixes(unittest.TestCase):
    """Fix 6 (overlay cleared on producer death) and fix 7 (no
    credential echo), exercised on the real bridge module with the
    browser side stubbed out."""

    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp()
        os.environ['GLAZYR_TRACE_DIR'] = cls.d
        import glazyr_mcp as gm
        cls.gm = gm

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.d)

    def test_guard_clears_overlay_and_invalidates(self):
        gm = self.gm
        gm._session_var.set('test.1')
        gm._state.update(apc=object(), seq=5, doc_key='DOC1', page_url='http://a/',
                         epoch='1.1', overlay={}, assertions={})
        n = gm._trace_action('type', target={'desc': '[7] input'}, value='hello')
        gm._state['trace_action'] = n
        gm._overlay_set(7, 'value', "value='hello' (bridge-asserted)")
        gm._state['trace_action'] = None
        ver = gm._state['overlay_version']
        orig = (gm.producer_health, gm._force_relaunch)
        gm.producer_health = lambda force=False: {'status': 'dead', 'via': 'popen',
                                                  'detail': 'browser exited'}
        gm._force_relaunch = lambda: True
        try:
            err = gm._guard_state_fresh()
        finally:
            gm.producer_health, gm._force_relaunch = orig
        self.assertIn('GLAZYR_PRODUCER_DEAD', err)
        self.assertEqual(gm._state['overlay'], {})
        self.assertEqual(gm._state['overlay_version'], ver + 1)
        self.assertEqual(gm._state['assertions'], {})
        evs = [json.loads(l) for l in open(gm._tracer().path, encoding='utf-8')]
        inv = [e for e in evs if e['kind'] == 'invalidation']
        self.assertEqual(inv[-1]['reason'], 'producer_dead')
        self.assertEqual([d['n'] for d in inv[-1]['dropped']], [n])
        relaunch = [e for e in evs if e.get('action') == 'relaunch_browser']
        self.assertEqual(relaunch[-1]['cause'], 'bridge_recovery')
        self.assertEqual(gt.verify_file(gm._tracer().path)['chain'], 'ok')

    def test_credential_predicate(self):
        gf = self.gm.gf
        fc = gf.cq.FormControlData()
        fc.form_control_type = gf.cq.FormControlType.Value('FORM_CONTROL_TYPE_INPUT_PASSWORD')
        self.assertTrue(gf.is_credential_control(fc))
        fc2 = gf.cq.FormControlData()
        fc2.form_control_type = gf.cq.FormControlType.Value('FORM_CONTROL_TYPE_INPUT_TEXT')
        fc2.field_name = 'customer'
        self.assertFalse(gf.is_credential_control(fc2))
        fc2.field_name = 'one_time_code'
        self.assertTrue(gf.is_credential_control(fc2))

    def test_tokenizer_recorded(self):
        self.assertIn(self.gm.TOKENIZER, ('cl100k', 'chars/4'))
        self.assertEqual(self.gm.VERSION, '0.3.4')


if __name__ == '__main__':
    unittest.main(verbosity=2)
