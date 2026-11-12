"""
RTTY demodulator (Embedded Python Block)

Stream input: float audio at samp_rate (the radio's USB audio, via an Audio Source).
Message inputs:
  status - connect to the Icom CI-V PTT block's status output. Decoding pauses while
           the radio is not in RX, and for holdoff_s after it returns to RX.
Message outputs:
  text    - received text, one display line per message (connect to the Console
            Router's rx_rtty input).
  preview - the last preview_chars received characters (line breaks as spaces), sent
            as each character arrives (connect to a QT GUI Message Edit Box's val input).

Radio: LSB-D, tuned so the signal's mark/space fall on 2125/2295 Hz. Tick reverse for an
inverted signal. Needs rtty.py next to the .grc file (or set lib_dir).
"""

import sys
import threading
import time

import numpy as np
import pmt
from gnuradio import gr


class blk(gr.sync_block):
    """RTTY receiver: 45.45 baud, 170 Hz shift, Baudot (US-TTY figures).

    reverse: swap mark and space (can be changed while running)
    squelch: minimum character quality 0-1 (0 = off; 0.6 hides most noise)
    usos: unshift to letters on space
    line_width: wrap received text at this many characters
    holdoff_s: keep decoding paused this long after the radio returns to RX
    preview_chars: length of the scrolling preview (0 = no preview messages)
    """

    def __init__(self, samp_rate=48000, baud=45.45, mark_hz=2125.0, space_hz=2295.0,
                 reverse=False, squelch=0.6, usos=True, line_width=64, holdoff_s=0.5,
                 preview_chars=80, lib_dir=''):
        gr.sync_block.__init__(self, name='RTTY Demod', in_sig=[np.float32], out_sig=None)
        self._cfg = dict(samp_rate=float(samp_rate), baud=float(baud), mark=float(mark_hz),
                         space=float(space_hz), squelch=float(squelch), usos=bool(usos),
                         width=int(line_width), lib_dir=lib_dir)
        self._reverse = bool(reverse)
        self._holdoff = float(holdoff_s)
        self._preview_chars = int(preview_chars)
        self._preview = ""
        self._demod = None
        self._lines = None
        self._rx = True
        self._resume = 0.0
        self._lock = threading.Lock()
        self.message_port_register_in(pmt.intern('status'))
        self.set_msg_handler(pmt.intern('status'), self._handle_status)
        self.message_port_register_out(pmt.intern('text'))
        self.message_port_register_out(pmt.intern('preview'))

    @property
    def reverse(self):
        return self._reverse

    @reverse.setter
    def reverse(self, value):
        self._reverse = bool(value)
        with self._lock:
            if self._demod is not None:
                self._demod.reverse = self._reverse

    def start(self):
        c = self._cfg
        if c['lib_dir'] and c['lib_dir'] not in sys.path:
            sys.path.insert(0, c['lib_dir'])
        import rtty
        with self._lock:
            self._demod = rtty.RttyDemodulator(c['samp_rate'], c['baud'], c['mark'], c['space'],
                                               self._reverse, c['squelch'], c['usos'])
            self._lines = rtty.LineAssembler(c['width'])
        return True

    def _handle_status(self, msg):
        v = pmt.to_python(msg)
        state = str(v[1] if isinstance(v, tuple) else v)
        rx = state.startswith("RX")
        with self._lock:
            if rx and not self._rx:
                self._resume = time.monotonic() + self._holdoff
            self._rx = rx

    def _publish(self, lines):
        for line in lines:
            self.message_port_pub(pmt.intern('text'), pmt.intern(line))

    def work(self, input_items, output_items):
        audio = input_items[0]
        now = time.monotonic()
        text = ""
        with self._lock:
            if self._demod is None:
                return len(audio)
            if not self._rx or now < self._resume:
                self._demod.reset()            # own transmission: nothing to decode
                lines = self._lines.poll(now + self._lines.idle)   # flush what we had
            else:
                text = self._demod.receive(audio)
                lines = self._lines.feed(text, now) + self._lines.poll(now)
        self._publish(lines)
        if text and self._preview_chars > 0:
            chars = text.replace("\r", "").replace("\n", " ")
            if chars:
                self._preview = (self._preview + chars)[-self._preview_chars:]
                self.message_port_pub(pmt.intern('preview'), pmt.intern(self._preview))
        return len(audio)
