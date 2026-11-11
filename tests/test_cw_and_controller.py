"""Tests for CW helpers and TxController (simulated radio)."""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import icom_civ as civ  # noqa: E402
import civ_controller as ctl  # noqa: E402
from test_icom_civ import FakeRadio  # noqa: E402


class TestCwHelpers(unittest.TestCase):
    def test_paris_timing(self):
        # standard word "PARIS " = 50 units; at 20 WPM one unit is 60 ms
        d = civ.cw_duration("PARIS PARIS", 20) - civ.cw_duration("PARIS", 20)
        self.assertAlmostEqual(d, 3.0, places=6)

    def test_prosign_shorter(self):
        self.assertLess(civ.cw_duration("^AR", 20), civ.cw_duration("AR", 20))
        self.assertAlmostEqual(civ.cw_duration("^AR", 20) + 2 * 0.06, civ.cw_duration("AR", 20))

    def test_validate(self):
        self.assertEqual(civ.normalize_cw("cq  de n0call"), "CQ DE N0CALL")
        with self.assertRaises(ValueError):
            civ.normalize_cw("hello!")

    def test_split(self):
        text = "CQ CQ CQ DE N0CALL N0CALL N0CALL PSE K ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        chunks = civ.split_cw(text)
        self.assertTrue(all(len(c) <= 30 for c in chunks))
        self.assertEqual("".join(chunks), text)

    def test_wpm_bcd(self):
        self.assertEqual(civ.wpm_to_civ(6), b"\x00\x00")
        self.assertEqual(civ.wpm_to_civ(48), b"\x02\x55")
        self.assertEqual(civ.wpm_to_civ(27), b"\x01\x28")   # 127.5 -> 128

    def test_set_mode_frames(self):
        r = FakeRadio()
        with civ.CivPort(serial_port=r, timeout=0.2) as c:
            c.set_mode("USB", data=True, filt=1)
            c.set_mode("CW")
            with self.assertRaises(ValueError):
                c.set_mode("CW", data=True)
        self.assertEqual(r.settings, [b"\x06\x01\x01", b"\x1a\x06\x01\x01", b"\x06\x03\x01"])


class Recorder:
    def __init__(self):
        self.states, self.mutes, self.errors = [], [], []
        self.cond = threading.Condition()

    def state(self, s):
        with self.cond:
            self.states.append(s)
            self.cond.notify_all()

    def wait_state(self, s, n=1, timeout=2.0):
        with self.cond:
            return self.cond.wait_for(lambda: self.states.count(s) >= n, timeout)


def make(radio=None, **kw):
    radio = radio or FakeRadio()
    port = civ.CivPort(serial_port=radio, timeout=0.2, retries=0).open()
    rec = Recorder()
    c = ctl.TxController(port, on_state=rec.state, on_mute=rec.mutes.append,
                         on_error=rec.errors.append, **kw)
    c.start()
    return radio, port, c, rec


class TestController(unittest.TestCase):
    def tearDown(self):
        self.c.close()
        self.port.close()

    def test_ptt_sequence(self):
        self.r, self.port, self.c, rec = make(lead=0.05, tail=0.05)
        self.c.ptt(True)
        self.assertTrue(rec.wait_state(ctl.TX))
        self.assertTrue(self.r.tx)
        self.assertEqual(rec.mutes[-1], False)
        self.c.ptt(False)
        self.assertTrue(rec.wait_state(ctl.RX, 2))
        self.assertFalse(self.r.tx)
        self.assertEqual(rec.states, [ctl.RX, ctl.LEAD, ctl.TX, ctl.TAIL, ctl.RX])
        self.assertEqual(rec.mutes, [True, False, True])
        self.assertEqual(rec.errors, [])

    def test_watchdog(self):
        self.r, self.port, self.c, rec = make(lead=0.0, tx_timeout=0.2)
        self.c.ptt(True)
        self.assertTrue(rec.wait_state(ctl.RX, 2))
        self.assertFalse(self.r.tx)
        self.assertIn("timeout", rec.errors[0])

    def test_radio_offline(self):
        self.r, self.port, self.c, rec = make(FakeRadio(silent=True))
        self.c.ptt(True)
        self.assertTrue(rec.wait_state(ctl.RX, 2))
        self.assertTrue(rec.errors)
        self.assertNotIn(ctl.TX, rec.states)

    def test_close_unkeys(self):
        self.r, self.port, self.c, rec = make(lead=0.0)
        self.c.ptt(True)
        self.assertTrue(rec.wait_state(ctl.TX))
        self.c.close()
        self.assertFalse(self.r.tx)

    def test_cw(self):
        self.r, self.port, self.c, rec = make(cw_wpm=40)
        self.c.cw("e e")            # ~0.27 s at 40 WPM
        self.assertTrue(rec.wait_state(ctl.CW))
        self.assertTrue(rec.wait_state(ctl.RX, 2))
        self.assertEqual(self.r.cw, [b"E E"])
        self.assertIn(b"\x14\x0c" + civ.wpm_to_civ(40), self.r.settings)

    def test_cw_rejected_during_ptt(self):
        self.r, self.port, self.c, rec = make(lead=0.0)
        self.c.ptt(True)
        self.assertTrue(rec.wait_state(ctl.TX))
        self.c.cw("TEST")
        time.sleep(0.1)
        self.assertEqual(self.r.cw, [])
        self.assertIn("CW ignored", rec.errors[0])


if __name__ == "__main__":
    unittest.main()
