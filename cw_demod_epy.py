"""
CW decoder (Embedded Python Block)

Stream input: float audio at samp_rate (the radio's USB audio, via an Audio Source).
Message inputs:
  status - connect to the Icom CI-V PTT block's status output. Decoding pauses while
           the radio is not in RX, and for holdoff_s after it returns to RX.
Message outputs:
  text   - received text, one display line per message (connect to the Console
           Router's rx_cw input).

Radio: CW mode; pitch_hz should match the radio's CW Pitch (AFC follows tones within
afc_hz of it). Needs cw_rx.py, icom_civ.py and rtty.py next to the .grc (or set lib_dir).
"""

import sys
import threading
import time

import numpy as np
import pmt
from gnuradio import gr


class blk(gr.sync_block):
    """CW receiver: tone detector with AFC, adaptive speed (5-60 WPM), squelch.

    pitch_hz: radio CW Pitch
    afc_hz: follow the strongest tone within this distance of pitch_hz (0 = off)
    wpm: starting speed guess (the decoder adapts to the sender)
    squelch: signal presence threshold (noise alone gives 3.8-5.8; default 6.5)
    line_width: wrap received text at this many characters
    holdoff_s: keep decoding paused this long after the radio returns to RX
    """

    def __init__(self, samp_rate=48000, pitch_hz=600.0, afc_hz=150.0, wpm=20.0, squelch=6.5,
                 line_width=64, holdoff_s=0.5, lib_dir=''):
        gr.sync_block.__init__(self, name='CW Decoder', in_sig=[np.float32], out_sig=None)
        self._cfg = dict(samp_rate=float(samp_rate), pitch=float(pitch_hz), afc=float(afc_hz),
                         wpm=float(wpm), squelch=float(squelch), width=int(line_width),
                         lib_dir=lib_dir)
        self._holdoff = float(holdoff_s)
        self._dec = None
        self._lines = None
        self._rx = True
        self._resume = 0.0
        self._paused = False
        self._lock = threading.Lock()
        self.message_port_register_in(pmt.intern('status'))
        self.set_msg_handler(pmt.intern('status'), self._handle_status)
        self.message_port_register_out(pmt.intern('text'))

    def start(self):
        c = self._cfg
        if c['lib_dir'] and c['lib_dir'] not in sys.path:
            sys.path.insert(0, c['lib_dir'])
        import cw_rx
        import rtty
        with self._lock:
            self._dec = cw_rx.CwDecoder(c['samp_rate'], c['pitch'], c['afc'], c['wpm'], c['squelch'])
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

    def work(self, input_items, output_items):
        audio = input_items[0]
        now = time.monotonic()
        with self._lock:
            if self._dec is None:
                return len(audio)
            if not self._rx or now < self._resume:
                # own transmission: skip the audio; speed and levels are kept
                self._paused = True
                lines = self._lines.poll(now + self._lines.idle)
            else:
                if self._paused:
                    self._dec.interrupt()
                    self._paused = False
                lines = self._lines.feed(self._dec.receive(audio), now) + self._lines.poll(now)
        for line in lines:
            self.message_port_pub(pmt.intern('text'), pmt.intern(line))
        return len(audio)
