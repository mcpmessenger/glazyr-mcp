#!/usr/bin/env python3
"""Repro drill (unit level) for the dead-browser-served-as-live path.

0.3.3 / f5763e3: every read calls ensure_browser() before the guard. A
dead browser (CDP down) is silently replaced there; producer_health()
then sees the NEW browser alive, so the guard never fires. If the new
producer's first write has the same seq as the state in hand,
refresh_apc() (keyed on seq only) never re-parses, and the dead
browser's page - with its bridge overlay - is served as live.

The producer swap is simulated with two real live pids (this process
and its parent: distinct, alive, distinct /proc start times -> distinct
epochs) behind stubbed read_payload / find_sem. Everything from
do_peek / glazyr_type down is the real bridge code.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'bridge'))


def _page(cq, url, title, doc):
    apc = cq.AnnotatedPageContent()
    fd = apc.main_frame_data
    fd.url, fd.title = url, title
    fd.document_identifier.serialized_token = doc
    root = apc.root_node
    root.content_attributes.attribute_type = cq.ContentAttributeType.Value('CONTENT_ATTRIBUTE_ROOT')
    root.content_attributes.common_ancestor_dom_node_id = 1
    c = root.children_nodes.add()
    a = c.content_attributes
    a.attribute_type = cq.ContentAttributeType.Value('CONTENT_ATTRIBUTE_FORM_CONTROL')
    a.common_ancestor_dom_node_id = 18
    a.form_control_data.field_name = 'user'
    a.form_control_data.form_control_type = cq.FormControlType.Value('FORM_CONTROL_TYPE_INPUT_TEXT')
    return apc.SerializeToString()


class DeadBrowserServedAsLive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp()
        os.environ['GLAZYR_TRACE_DIR'] = cls.d
        import glazyr_mcp as gm
        cls.gm = gm
        gm._tracer_box['w'] = None      # fresh writer in this class's dir
        cls.orig = {k: getattr(gm, k) for k in
                    ('ensure_browser', 'read_payload', 'find_sem', '_force_relaunch')}

    @classmethod
    def tearDownClass(cls):
        for k, v in cls.orig.items():
            setattr(cls.gm, k, v)
        shutil.rmtree(cls.d)

    def setUp(self):
        gm = self.gm
        self.cur = {}
        self.relaunches = []
        gm.ensure_browser = lambda: True

        def read_payload():
            gm._last_payload_pid[0] = self.cur.get('pid')
            if not self.cur:
                return 0, None
            return self.cur['seq'], self.cur['bytes']
        gm.read_payload = read_payload
        gm.find_sem = lambda: (('/fake', self.cur['pid']) if self.cur else (None, None))
        gm._force_relaunch = lambda: self.relaunches.append(time.time()) or True
        gm._state.update(browser=None, apc=None, seq=0, seq_at=0, nodes={}, sigs={},
                         prev_sigs={}, overlay={}, assertions={}, doc_key=None,
                         page_url='', page_title='', last_served=None, epoch=None,
                         probe_cache=(0.0, None), producer_died=None,
                         served_history=[], trace_action=None)
        gm._fresh.update(status=None, dead_epoch=None)
        gm._session_var.set(f'drill.{self._testMethodName}')
        self.cq = gm.cq
        self.A = os.getpid()
        self.B = os.getppid()

    def producer(self, pid, seq, url, title, doc):
        self.cur.update(pid=pid, seq=seq, bytes=_page(self.cq, url, title, doc))

    def events(self):
        w = self.gm._tracer()
        sid = self.gm._cur_session()
        return [json.loads(l) for l in open(w.path, encoding='utf-8')
                if json.loads(l)['session'] in (sid, self.gm._bridge_session())]

    def _typed_state_on_A(self, seq=1):
        """Browser A shows the secret form; the agent typed into [18]."""
        gm = self.gm
        self.producer(self.A, seq, 'http://bank.example/transfer', 'Transfer', 'DOC-A')
        out = gm.do_peek()
        self.assertIn('bank.example/transfer', out)
        n = gm._trace_action('type', target={'desc': '[18] input'}, value='ada')
        gm._state['trace_action'] = n
        gm._overlay_set(18, 'value', "value='ada' (bridge-asserted)")
        gm._state['trace_action'] = None
        gm._trace_outcome(n, 'asserted', status='ok')
        return n

    def _assert_no_stale_reads(self, dead_epoch):
        """Trace invariant: after the producer is known dead, no served
        read carries the dead producer's epoch."""
        dead_seen = False
        for e in self.events():
            if e['kind'] == 'freshness' and e['status'] == 'dead':
                dead_seen = True
            if dead_seen and e['kind'] == 'state_read' and 'error' not in e:
                self.assertNotEqual((e.get('state') or {}).get('epoch'), dead_epoch,
                                    f'stale read served after death: n={e["n"]}')
        self.assertTrue(dead_seen, 'no freshness dead event')

    def test_same_seq_replacement_read(self):
        gm = self.gm
        n = self._typed_state_on_A(seq=1)
        dead_epoch = gm._state['epoch']
        # Browser A dies; ensure_browser's replacement B writes about:blank at seq 1.
        self.producer(self.B, 1, 'about:blank', '', 'DOC-B')
        out = gm.do_peek()
        self.assertNotIn('# url: http://bank.example', out, 'dead browser page served as live')
        self.assertTrue(out.startswith('Glazyr error GLAZYR_PRODUCER_DEAD'), out)
        self.assertNotIn("value='ada'", out, 'dead browser overlay served as live')
        self.assertIn('GLAZYR_PRODUCER_DEAD', out)
        self.assertEqual(gm._state['overlay'], {})
        self.assertEqual(self.relaunches, [], 'browser was already replaced; no second relaunch')
        inv = [e for e in self.events() if e['kind'] == 'invalidation']
        self.assertEqual(inv[-1]['reason'], 'producer_dead')
        self.assertEqual([d['n'] for d in inv[-1]['dropped']], [n])
        # The next read is the fresh browser's real state.
        out2 = gm.do_peek()
        self.assertIn('about:blank', out2)
        self.assertNotIn("value='ada'", out2)
        self._assert_no_stale_reads(dead_epoch)
        kinds = [(e['kind'], e.get('status')) for e in self.events()]
        self.assertIn(('freshness', 'recovered'), kinds)

    def test_different_seq_replacement_read_is_explicit(self):
        gm = self.gm
        self._typed_state_on_A(seq=3)
        self.producer(self.B, 1, 'about:blank', '', 'DOC-B')
        out = gm.do_peek()
        self.assertIn('GLAZYR_PRODUCER_DEAD', out)
        inv = [e for e in self.events() if e['kind'] == 'invalidation']
        self.assertEqual(inv[-1]['reason'], 'producer_dead')

    def test_same_seq_replacement_action(self):
        gm = self.gm
        self._typed_state_on_A(seq=1)
        dead_epoch = gm._state['epoch']
        self.producer(self.B, 1, 'about:blank', '', 'DOC-B')
        out = gm.glazyr_type(18, 'more')       # guard runs before any CDP call
        self.assertIn('GLAZYR_PRODUCER_DEAD', out)
        self.assertEqual(gm._state['overlay'], {})
        self._assert_no_stale_reads(dead_epoch)

    def test_same_producer_new_seq_is_not_a_death(self):
        gm = self.gm
        self._typed_state_on_A(seq=1)
        self.producer(self.A, 2, 'http://bank.example/transfer', 'Transfer', 'DOC-A')
        out = gm.do_peek()
        self.assertNotIn('GLAZYR_PRODUCER_DEAD', out)
        self.assertIn("value='ada'", out)     # same document: the overlay stands


if __name__ == '__main__':
    unittest.main(verbosity=2)
