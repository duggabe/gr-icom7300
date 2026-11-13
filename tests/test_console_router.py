"""Console Router in a flowgraph, driven over ZMQ the way gr-webserver v2.1 does it:
keyboard lines published as hand-built PMT symbols, display lines read back from SUB."""

import importlib.util
import os
import socket
import sys
import time
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")

try:
    import pmt
    import zmq
    from gnuradio import blocks, gr, zeromq
except ImportError:  # pragma: no cover
    gr = None


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def webserver_pmt(text):
    """What gr-webserver's string_to_pmt() sends: PST_SYMBOL, 0x00, one length byte, text."""
    return b"\x02\x00" + bytes([len(text)]) + text.encode()


def webserver_text(data):
    """What gr-webserver's pmt_to_string() shows for a symbol."""
    assert data[0] == 2
    return data[3:].decode()


@unittest.skipIf(gr is None, "GNU Radio not available")
class TestConsoleRouter(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("router", os.path.join(ROOT, "console_router_epy.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        in_port, out_port = free_port(), free_port()

        self.ctx = zmq.Context()
        self.kbd = self.ctx.socket(zmq.PUB)                 # webserver's out_sock (binds)
        self.kbd.bind(f"tcp://127.0.0.1:{in_port}")
        self.screen = self.ctx.socket(zmq.SUB)              # webserver's in_sock (connects)
        self.screen.connect(f"tcp://127.0.0.1:{out_port}")
        self.screen.setsockopt(zmq.SUBSCRIBE, b"")
        self.screen.setsockopt(zmq.RCVTIMEO, 2000)

        self.tb = gr.top_block()
        self.router = mod.blk(**self.router_args())
        src = zeromq.sub_msg_source(f"tcp://127.0.0.1:{in_port}", 100, False)
        sink = zeromq.pub_msg_sink(f"tcp://127.0.0.1:{out_port}", 100, True)
        self.cw, self.rtty = blocks.message_debug(), blocks.message_debug()
        self.radio = blocks.message_debug()
        self.tb.msg_connect(src, "out", self.router, "in")
        self.tb.msg_connect(self.router, "display", sink, "in")
        self.tb.msg_connect(self.router, "cw", self.cw, "store")
        self.tb.msg_connect(self.router, "rtty", self.rtty, "store")
        self.tb.msg_connect(self.router, "radio", self.radio, "store")
        self.tb.start()
        time.sleep(0.5)                                     # let ZMQ connect (slow joiner)
        self.lines = []

    def router_args(self):
        return dict(mode="RTTY")

    def tearDown(self):
        self.tb.stop()
        self.tb.wait()
        self.kbd.close()
        self.screen.close()
        self.ctx.term()

    def type(self, text):
        self.kbd.send(webserver_pmt(text))
        time.sleep(0.2)

    def post(self, port, text):
        self.router.to_basic_block()._post(pmt.intern(port), pmt.intern(text))
        time.sleep(0.2)

    def screen_lines(self):
        try:
            while True:
                self.lines.append(webserver_text(self.screen.recv(zmq.NOBLOCK)))
        except zmq.Again:
            pass
        return self.lines

    @staticmethod
    def stored(dbg):
        return [pmt.symbol_to_string(dbg.get_message(i)) for i in range(dbg.num_messages())]

    def test_routing_and_commands(self):
        self.type("CQ CQ DE N0CALL")
        self.type("/cw")
        self.type("TEST")
        self.type("/stop")
        self.type("/RTTY")
        self.type("73")
        self.type("/bogus")
        self.assertEqual(self.stored(self.rtty), ["CQ CQ DE N0CALL", "", "73"])
        self.assertEqual(self.stored(self.cw), ["TEST", ""])
        self.assertEqual(self.stored(self.radio), ["CW DECODE", "LSB-D"])   # /cw, /RTTY
        time.sleep(0.6)
        lines = self.screen_lines()
        for want in ("* Mode RTTY. Type /help for commands.", "* Mode CW", "* Stop", "* Mode RTTY", "* Unknown command /bogus. Type /help."):
            self.assertIn(want, lines)

    def test_received_text_follows_mode(self):
        self.post("rx_cw", "CW BEFORE")
        self.type("/cw")
        self.post("rx_cw", "CQ TEST")
        self.post("rx_rtty", "RTTY HIDDEN")
        self.type("/rtty")
        self.post("rx_rtty", "RYRY")
        time.sleep(0.6)
        lines = [l for l in self.screen_lines() if not l.startswith("* ")]
        self.assertEqual(lines, ["CQ TEST", "RYRY"])

    def test_status_display(self):
        for s in ("LEAD", "TX", "TAIL", "RX", "RX | ERROR: no reply", "RX", "CW", "RX"):
            self.post("status", s)
        self.post("info", "RTTY: skipped unsupported characters '<!>'")
        self.post("rx_rtty", "CQ CQ DE N0CALL")
        self.post("rx_cw", "NOT SHOWN IN RTTY MODE")
        lines = [l for l in self.screen_lines() if not l.startswith("* Mode ")]   # greeting
        self.assertEqual(lines, ["TRACE: TX", "TRACE: RX", "* ERROR: no reply",
                                 "TRACE: TX (CW keyer)", "TRACE: RX",
                                 "* RTTY: skipped unsupported characters '&lt;!>'",
                                 "CQ CQ DE N0CALL"])


class TestConsoleRouterDefault(TestConsoleRouter):
    """Default mode CW: the greeting also sets up the radio's CW decoder."""

    def router_args(self):
        return {}

    def test_routing_and_commands(self):
        time.sleep(1.0)                                     # greeting timer
        self.assertIn("* Mode CW. Type /help for commands.", self.screen_lines())
        self.assertEqual(self.stored(self.radio), ["CW DECODE"])
        self.type("CQ TEST")
        self.assertEqual(self.stored(self.cw), ["CQ TEST"])

    test_received_text_follows_mode = None
    test_status_display = None


if __name__ == "__main__":
    unittest.main()
