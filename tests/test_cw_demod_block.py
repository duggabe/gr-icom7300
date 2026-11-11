"""CW Decoder Embedded Python Block in a flowgraph: audio in, text lines out."""

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

from test_cw_rx import FS, keyed  # noqa: E402


@unittest.skipIf(gr is None, "GNU Radio not available")
class TestCwBlock(unittest.TestCase):
    def run_graph(self, audio, status=None):
        spec = importlib.util.spec_from_file_location("cwb", os.path.join(ROOT, "cw_demod_epy.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        tb = gr.top_block()
        src = blocks.vector_source_f(audio.astype(np.float32).tolist(), False)
        dec = mod.blk(samp_rate=FS)
        dbg = blocks.message_debug()
        tb.connect(src, dec)
        tb.msg_connect(dec, "text", dbg, "store")
        if status:
            dec.to_basic_block()._post(pmt.intern("status"), pmt.intern(status))
        tb.start()
        tb.wait()
        time.sleep(0.2)
        return [pmt.symbol_to_string(dbg.get_message(i)) for i in range(dbg.num_messages())]

    def test_lines_out(self):
        # LineAssembler flushes the partial line only after 2 s idle (wall clock), so the
        # graph's end-of-stream gives no line unless one is full: send > 64 characters
        text = "CQ CQ DE N0CALL N0CALL N0CALL PSE K TEST TEST TEST THE QUICK BROWN FOX"
        lines = self.run_graph(keyed(text, 30))
        self.assertTrue(lines and "CQ DE N0CALL N0CALL" in lines[0], lines)

    def test_paused_while_transmitting(self):
        text = "CQ CQ DE N0CALL N0CALL N0CALL PSE K TEST TEST TEST THE QUICK BROWN FOX"
        self.assertEqual(self.run_graph(keyed(text, 30), status="TX"), [])


if __name__ == "__main__":
    unittest.main()
