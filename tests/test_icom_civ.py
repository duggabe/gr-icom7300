"""Tests for icom_civ against a simulated radio (no hardware needed).

Run:  python3 -m unittest discover -s tests -v
"""

import os
import queue
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import icom_civ as civ  # noqa: E402


class FakeRadio:
    """Serial-like object that acts like an IC-7300 on CI-V."""

    def __init__(self, addr=0x94, echo=False, nak=False, silent=False, transceive_noise=False):
        self.addr = addr
        self.echo = echo
        self.nak = nak
        self.silent = silent
        self.transceive_noise = transceive_noise
        self.tx = False
        self.writes = []
        self.cw = []
        self.settings = []
        self._out = queue.Queue()
        self._parser = civ.FrameParser()

    def write(self, data):
        self.writes.append(bytes(data))
        if self.echo:
            self._out.put(bytes(data))
        for f in self._parser.feed(data):
            if f.to != self.addr or self.silent:
                continue
            if self.transceive_noise:
                # unsolicited frequency broadcast to 00, sent before the reply
                self._out.put(civ.build_frame(0x00, self.addr, bytes([0x00, 0x00, 0x00, 0x07, 0x14, 0x00])))
            self._out.put(self._respond(f))

    def _respond(self, f):
        p = f.payload
        reply = lambda payload: civ.build_frame(f.frm, self.addr, payload)
        if self.nak:
            return reply(bytes([civ.NAK]))
        if p[:2] == bytes([civ.CMD_PTT, civ.SUB_PTT]):
            if len(p) == 3:
                self.tx = p[2] == 0x01
                return reply(bytes([civ.ACK]))
            return reply(bytes([civ.CMD_PTT, civ.SUB_PTT, 0x01 if self.tx else 0x00]))
        if p[:2] == bytes([civ.CMD_READ_ID, civ.SUB_READ_ID]):
            return reply(bytes([civ.CMD_READ_ID, civ.SUB_READ_ID, self.addr]))
        if p[0] == civ.CMD_SEND_CW and 2 <= len(p) <= 31:
            self.cw.append(p[1:])
            return reply(bytes([civ.ACK]))
        if p[:2] in (bytes([civ.CMD_LEVEL, civ.SUB_KEY_SPEED]), bytes([civ.CMD_FUNC, civ.SUB_BREAK_IN]),
                     bytes([civ.CMD_EXT, civ.SUB_DATA_MODE])) or p[0] == civ.CMD_SET_MODE:
            self.settings.append(p)
            return reply(bytes([civ.ACK]))
        return reply(bytes([civ.NAK]))

    def read(self, n):
        try:
            return self._out.get(timeout=0.02)
        except queue.Empty:
            return b""

    def close(self):
        pass


def port(radio, **kw):
    kw.setdefault("timeout", 0.2)
    return civ.CivPort(serial_port=radio, radio_addr=radio.addr, **kw)


class TestParser(unittest.TestCase):
    def test_split_chunks_and_noise(self):
        p = civ.FrameParser()
        raw = b"\x00\x11" + civ.build_frame(0xE0, 0x94, b"\xfb") + b"\xfe\xfe\xfe\xe0\x94\x1c\x00\x01\xfd"
        frames = []
        for b in raw:  # one byte at a time
            frames += p.feed(bytes([b]))
        self.assertEqual([f.payload for f in frames], [b"\xfb", b"\x1c\x00\x01"])

    def test_jam_dropped(self):
        self.assertEqual(civ.FrameParser().feed(b"\xfe\xfe\xfc\xfd"), [])


class TestPort(unittest.TestCase):
    def test_ptt_cycle(self):
        r = FakeRadio()
        with port(r) as c:
            self.assertFalse(c.get_ptt())
            c.set_ptt(True)
            self.assertTrue(r.tx)
            self.assertTrue(c.get_ptt())
            c.set_ptt(False)
            self.assertFalse(c.get_ptt())
        self.assertEqual(r.writes[1], bytes.fromhex("FEFE94E01C0001FD"))

    def test_echo_and_transceive_ignored(self):
        seen = []
        r = FakeRadio(echo=True, transceive_noise=True)
        with port(r, on_unsolicited=seen.append) as c:
            c.set_ptt(True)
            self.assertTrue(c.get_ptt())
        self.assertTrue(seen)  # broadcasts went to the callback, not the reply path

    def test_read_id_other_address(self):
        r = FakeRadio(addr=0xB6)
        with port(r) as c:
            self.assertEqual(c.read_id(), 0xB6)

    def test_nak(self):
        with port(FakeRadio(nak=True)) as c:
            with self.assertRaises(civ.CivNak):
                c.set_ptt(True)

    def test_timeout_retries(self):
        r = FakeRadio(silent=True)
        with port(r, retries=2) as c:
            with self.assertRaises(civ.CivTimeout):
                c.set_ptt(True)
        self.assertEqual(len(r.writes), 3)

    def test_wrong_address_times_out(self):
        r = FakeRadio(addr=0xB6)
        with civ.CivPort(serial_port=r, radio_addr=0x94, timeout=0.1, retries=0) as c:
            with self.assertRaises(civ.CivTimeout):
                c.get_ptt()


if __name__ == "__main__":
    unittest.main()
