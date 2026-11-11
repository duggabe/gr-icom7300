"""
Console router (Embedded Python Block) for gr-webserver
(https://github.com/duggabe/gr-webserver, v2.1: ZMQ PUB/SUB).

Connect:
  ZMQ SUB Message Source (tcp://127.0.0.1:50251, bind False) -> in
  display -> ZMQ PUB Message Sink (tcp://127.0.0.1:50252, bind True)
  cw   -> Icom CI-V PTT block's cw input
  rtty -> RTTY AFSK Mod's text input
  Icom CI-V PTT status -> status,  RTTY AFSK Mod info -> info

Each line typed in the console is sent in the current mode. Commands:
  /cw  /rtty   switch mode
  /stop        abort the current CW or RTTY transmission (an empty line does the same)
  /help        list the commands
The console shows radio state changes (TX / RX), errors and notices.
"""

import threading

import pmt
from gnuradio import gr

MODES = ("RTTY", "CW")
HELP = ["Type a line and press Enter to send it in the current mode.",
        "/rtty or /cw: switch mode.  /stop or empty line: abort.  /help: this list."]
QUIET_STATES = ("LEAD", "TAIL")     # brief PTT sequencing states, not worth showing


def _text(msg):
    v = pmt.to_python(msg)
    if isinstance(v, tuple) and len(v) == 2:
        v = v[1]
    if isinstance(v, (bytes, bytearray)):
        v = bytes(v).decode("utf-8", "replace")
    return v if isinstance(v, str) else str(v)


class blk(gr.basic_block):
    """Routes gr-webserver console lines to CW or RTTY and shows radio status.

    mode: 'RTTY' or 'CW' at start; change it from the console with /rtty or /cw.
    """

    def __init__(self, mode='RTTY'):
        gr.basic_block.__init__(self, name='Console Router', in_sig=None, out_sig=None)
        m = str(mode).upper()
        self._mode = m if m in MODES else MODES[0]
        self._state = None
        self._error = None
        self._greet = threading.Timer(0, lambda: None)
        for port, handler in (("in", self._handle_in), ("status", self._handle_status),
                              ("info", self._handle_info)):
            self.message_port_register_in(pmt.intern(port))
            self.set_msg_handler(pmt.intern(port), handler)
        for port in ("display", "cw", "rtty"):
            self.message_port_register_out(pmt.intern(port))

    def start(self):
        # delayed: ZMQ PUB drops messages until the console's SUB socket has connected
        self._greet = threading.Timer(1.0, self._show, (f"Mode {self._mode}. Type /help for commands.",))
        self._greet.daemon = True
        self._greet.start()
        return True

    def stop(self):
        self._greet.cancel()
        return True

    def _show(self, line):
        # gr-webserver shows one line per message and renders it as HTML
        line = line.replace("\r", " ").replace("\n", " ").replace("&", "&amp;").replace("<", "&lt;")
        self.message_port_pub(pmt.intern("display"), pmt.intern(line))

    def _send(self, mode, text):
        self.message_port_pub(pmt.intern(mode.lower()), pmt.intern(text))

    def _handle_in(self, msg):
        line = _text(msg).strip()
        cmd = line.lower()
        if cmd in ("/rtty", "/cw"):
            self._mode = cmd[1:].upper()
            self._show(f"Mode {self._mode}")
        elif cmd in ("", "/stop"):
            for m in MODES:
                self._send(m, "")              # empty text = abort, in both blocks
            self._show("Stop")
        elif cmd == "/help":
            for h in HELP:
                self._show(h)
        elif cmd.startswith("/"):
            self._show(f"Unknown command {line}. Type /help.")
        else:
            self._send(self._mode, line)

    def _handle_status(self, msg):
        state, _, err = _text(msg).partition(" | ")
        if err.startswith("ERROR: "):
            err = err[len("ERROR: "):]
        if err and err != self._error:
            self._show(f"ERROR: {err}")
        self._error = err or None
        if state != self._state and state not in QUIET_STATES:
            self._show("TX (CW keyer)" if state == "CW" else state)
            self._state = state

    def _handle_info(self, msg):
        self._show(_text(msg))
