"""RTTY Demod Embedded Python Block in a flowgraph: audio in, text lines out, and
decoding paused while the radio transmits."""

import importlib.util
import os
import sys
import time
import unittest

import numpy as np

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

try:
    import pmt
    from gnuradio import blocks, gr
except ImportError:  # pragma: no cover
    gr = None

from test_rtty import FS, burst  # noqa: E402


def load():
    spec = importlib.util.spec_from_file_location("demod", os.path.join(ROOT, "rtty_demod_epy.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@unittest.skipIf(gr is None, "GNU Radio not available")
class TestDemodBlock(unittest.TestCase):
    def run_graph(self, audio, status=None, reverse=False):
        tb = gr.top_block()
        src = blocks.vector_source_f(audio.astype(np.float32).tolist(), False)
        demod = load().blk(samp_rate=FS, reverse=reverse)
        dbg = blocks.message_debug()
        tb.connect(src, demod)
        tb.msg_connect(demod, "text", dbg, "store")
        if status:
            demod.to_basic_block()._post(pmt.intern("status"), pmt.intern(status))
        tb.start()
        tb.wait()
        time.sleep(0.2)
        return demod, dbg

    @staticmethod
    def lines(dbg):
        return [pmt.symbol_to_string(dbg.get_message(i)) for i in range(dbg.num_messages())]

    def test_lines_out(self):
        audio = burst("\r\nCQ CQ DE N0CALL\r\nPSE K\r\n")
        _, dbg = self.run_graph(audio)
        self.assertEqual(self.lines(dbg), ["CQ CQ DE N0CALL", "PSE K"])

    def test_paused_while_transmitting(self):
        _, dbg = self.run_graph(burst("\r\nCQ CQ DE N0CALL\r\n"), status="TX")
        self.assertEqual(self.lines(dbg), [])

    def test_reverse_property(self):
        demod = load().blk(samp_rate=FS)
        demod.reverse = True                      # GRC callback before start
        self.assertTrue(demod.reverse)
        _, dbg = self.run_graph(burst("\r\nRYRY TEST\r\n", swap=True), reverse=True)
        self.assertEqual(self.lines(dbg), ["RYRY TEST"])


if __name__ == "__main__":
    unittest.main()
