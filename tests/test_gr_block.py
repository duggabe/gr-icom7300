"""End-to-end: the Embedded Python Block inside a GNU Radio flowgraph, talking through a
pseudo-terminal to the simulated radio (pyserial opens a real tty device)."""

import importlib.util
import os
import pty
import sys
import threading
import time
import tty
import unittest

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

try:
    import pmt
    from gnuradio import blocks, gr
except ImportError:  # pragma: no cover
    gr = None

from test_icom_civ import FakeRadio  # noqa: E402


class PtyRadio:
    """Bridges a FakeRadio onto the master side of a pty."""

    def __init__(self):
        self.radio = FakeRadio()
        self.master, slave = pty.openpty()
        tty.setraw(slave)
        self.path = os.ttyname(slave)
        self._slave = slave
        self._stop = False
        threading.Thread(target=self._rx, daemon=True).start()
        threading.Thread(target=self._tx, daemon=True).start()

    def _rx(self):
        while not self._stop:
            try:
                data = os.read(self.master, 64)
            except OSError:
                return
            self.radio.write(data)

    def _tx(self):
        while not self._stop:
            data = self.radio.read(64)
            if data:
                os.write(self.master, data)

    def close(self):
        self._stop = True
        os.close(self._slave)
        os.close(self.master)


def load_block_module():
    spec = importlib.util.spec_from_file_location("icom_ptt_epy", os.path.join(ROOT, "icom_ptt_epy.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@unittest.skipIf(gr is None, "GNU Radio not available")
class TestGrBlock(unittest.TestCase):
    def test_flowgraph(self):
        radio = PtyRadio()
        mod = load_block_module()
        tb = gr.top_block()
        blk = mod.blk(port=radio.path, lead_ms=20, tail_ms=20, tx_timeout_s=30, cw_wpm=40)
        status = blocks.message_debug()
        mute = blocks.message_debug()
        tb.msg_connect(blk, "status", status, "store")
        tb.msg_connect(blk, "mute", mute, "store")
        tb.start()
        try:
            def statuses():
                return [pmt.symbol_to_string(status.get_message(i)) for i in range(status.num_messages())]

            def wait(pred, timeout=3.0):
                end = time.time() + timeout
                while time.time() < end and not pred():
                    time.sleep(0.02)
                return pred()

            # exactly what QT GUI Toggle Button sends
            blk._post(pmt.intern("ptt"), pmt.cons(pmt.intern("value"), pmt.from_bool(True)))
            self.assertTrue(wait(lambda: "TX" in statuses()), statuses())
            self.assertTrue(radio.radio.tx)
            blk._post(pmt.intern("ptt"), pmt.cons(pmt.intern("value"), pmt.from_bool(False)))
            self.assertTrue(wait(lambda: statuses().count("RX") >= 2), statuses())
            self.assertFalse(radio.radio.tx)

            blk._post(pmt.intern("cw"), pmt.intern("cq test"))
            self.assertTrue(wait(lambda: statuses().count("RX") >= 3), statuses())
            self.assertEqual(radio.radio.cw, [b"CQ TEST"])

            blk.cw_wpm = 25          # GRC callback path
            self.assertTrue(wait(lambda: b"\x14\x0c" + __import__("icom_civ").wpm_to_civ(25)
                                 in radio.radio.settings))

            mutes = [pmt.to_python(mute.get_message(i)) for i in range(mute.num_messages())]
            self.assertEqual(mutes[:3], [True, False, True])
            self.assertEqual(statuses()[:5], ["RX", "LEAD", "TX", "TAIL", "RX"])
        finally:
            blk._post(pmt.intern("ptt"), pmt.cons(pmt.intern("value"), pmt.from_bool(True)))
            time.sleep(0.1)
            tb.stop()
            tb.wait()
            self.assertFalse(radio.radio.tx, "stop() must unkey")
            radio.close()


if __name__ == "__main__":
    unittest.main()
