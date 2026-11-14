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

    def test_level_mapping_matches_radio(self):
        # observed on the IC-7300MK2 menu display
        for level, pct in [(128, 50), (132, 51), (153, 60), (0, 0), (255, 100)]:
            self.assertEqual(civ.level_to_percent(level), pct)
        for pct in range(0, 101):
            self.assertEqual(civ.level_to_percent(civ.percent_to_level(pct)), pct)

    def test_bcd(self):
        self.assertEqual(civ.bcd4(81), b"\x00\x81")
        self.assertEqual(civ.from_bcd(b"\x01\x53"), 153)

    def test_usb_mod_level(self):
        r = FakeRadio()
        with civ.CivPort(serial_port=r, timeout=0.2) as c:
            self.assertEqual(c.get_usb_mod_level(), 50)
            self.assertEqual(c.set_usb_mod_level(45), 45)
        self.assertIn(bytes.fromhex("FEFEB6E01A050081") + civ.bcd4(civ.percent_to_level(45)) + b"\xfd",
                      r.writes)

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
        self.states, self.mutes, self.errors, self.infos = [], [], [], []
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
                         on_error=rec.errors.append, on_info=rec.infos.append, **kw)
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

    def test_usb_mod_level_coalesced(self):
        self.r, self.port, self.c, rec = make(usb_mod_level=40)
        for pct in range(41, 61):        # slider drag
            self.c.set_usb_mod_level(pct)
        deadline = time.time() + 2
        while time.time() < deadline and self.r.menu.get(b"\x00\x81") != civ.bcd4(civ.percent_to_level(60)):
            time.sleep(0.02)
        self.assertEqual(self.r.menu[b"\x00\x81"], civ.bcd4(civ.percent_to_level(60)))
        writes = [w for w in self.r.writes if w.startswith(bytes.fromhex("FEFEB6E01A050081")) and len(w) > 9]
        self.assertLess(len(writes), 21)  # first (40%) + coalesced updates, not one per step

    def test_usb_mod_nak_keeps_tx(self):
        self.r, self.port, self.c, rec = make(lead=0.0)
        self.c.ptt(True)
        self.assertTrue(rec.wait_state(ctl.TX))
        self.r.nak = True
        self.c.set_usb_mod_level(30)
        time.sleep(0.2)
        self.r.nak = False
        self.assertEqual(self.c.state, ctl.TX)
        self.assertTrue(self.r.tx)
        self.assertIn("rejected USB MOD", rec.errors[0])

    def test_cw_rejected_during_ptt(self):
        self.r, self.port, self.c, rec = make(lead=0.0)
        self.c.ptt(True)
        self.assertTrue(rec.wait_state(ctl.TX))
        self.c.cw("TEST")
        time.sleep(0.1)
        self.assertEqual(self.r.cw, [])
        self.assertIn("CW ignored", rec.errors[0])

    def wait_info(self, rec, timeout=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline and not (rec.infos or rec.errors):
            time.sleep(0.02)

    def test_cw_decode_setup(self):
        r = FakeRadio()
        r.mode = (0x01, 0x02)                     # USB, FIL2
        self.r, self.port, self.c, rec = make(r)
        self.c.setup_cw_decode()
        self.wait_info(rec)
        self.assertEqual(rec.errors, [])
        self.assertEqual(r.mode, (0x03, 0x03))    # CW, FIL3 by default
        self.assertEqual(r.menu[b"\x02\x64"], b"\x02")
        self.assertIn("CW mode, FIL3", rec.infos[0])

    def test_cw_decode_setup_cw_r_filter(self):
        r = FakeRadio()
        r.mode = (0x07, 0x01)                     # CW-R stays CW-R, FIL1 -> FIL3
        self.r, self.port, self.c, rec = make(r)
        self.c.setup_cw_decode()
        self.wait_info(rec)
        self.assertEqual(r.mode, (0x07, 0x03))
        self.assertIn("CW-R mode, FIL3", rec.infos[0])

    def test_cw_decode_setup_already_set(self):
        r = FakeRadio()
        r.mode = (0x03, 0x03)                     # already CW FIL3: no mode command
        self.r, self.port, self.c, rec = make(r)
        self.c.setup_cw_decode()
        self.wait_info(rec)
        self.assertEqual(r.mode, (0x03, 0x03))
        self.assertFalse(any(w[4:5] == bytes([civ.CMD_SET_MODE]) for w in r.writes))
        self.assertIn("CW mode, FIL3", rec.infos[0])

    def test_cw_decode_setup_other_filter(self):
        r = FakeRadio()
        r.mode = (0x01, 0x03)                     # USB FIL3; ask for FIL2
        self.r, self.port, self.c, rec = make(r)
        self.c.setup_cw_decode(filt=2)
        self.wait_info(rec)
        self.assertEqual(r.mode, (0x03, 0x02))
        with self.assertRaises(ValueError):
            self.c.setup_cw_decode(filt=4)

    def test_lsb_data_setup(self):
        r = FakeRadio()
        r.mode = (0x03, 0x02)                     # CW, FIL2
        self.r, self.port, self.c, rec = make(r)
        self.c.setup_data_mode("LSB")
        self.wait_info(rec)
        self.assertEqual(rec.errors, [])
        self.assertEqual(r.mode, (0x00, 0x02))    # LSB, filter kept
        self.assertEqual(r.settings[-1], b"\x1a\x06\x01\x02")   # DATA on, FIL2
        self.assertIn("LSB-D mode, FIL2", rec.infos[0])

    def test_cw_decode_setup_refused_while_tx(self):
        self.r, self.port, self.c, rec = make(lead=0.0)
        self.c.ptt(True)
        self.assertTrue(rec.wait_state(ctl.TX))
        self.c.setup_cw_decode()
        time.sleep(0.1)
        self.assertEqual(self.r.mode, (0x01, 0x01))
        self.assertIn("ignored", rec.errors[0])
        self.assertTrue(self.r.tx)


if __name__ == "__main__":
    unittest.main()
