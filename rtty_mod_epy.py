"""
RTTY AFSK modulator (Embedded Python Block)

Stream output: float audio at samp_rate (silence when idle).
Message inputs:
  text   - text to send (string, or pair from QT GUI Message Edit Box). Each message is
           sent on its own line. Text typed while sending is appended to the same burst.
  status - connect to the Icom CI-V PTT block's status output.
Message outputs:
  ptt    - connect to the Icom CI-V PTT block's ptt input. The modulator keys the radio,
           waits for TX, sends, then releases PTT, but only if it did the keying.
  info   - notices (unsupported characters skipped, radio did not key, ...).

Radio: LSB-D with mark 2125 / space 2295 Hz (normal on-air sense). For USB-D, swap them.
Needs rtty.py next to the .grc file (or set lib_dir).
"""

import sys
import threading

import numpy as np
import pmt
from gnuradio import gr


def _text(msg):
    v = pmt.to_python(msg)
    if isinstance(v, tuple) and len(v) == 2:
        v = v[1]
    return v if isinstance(v, str) else str(v)


class blk(gr.sync_block):
    """RTTY AFSK modulator: Baudot, 45.45 baud, 170 Hz shift, automatic PTT.

    preamble_s: mark idle after the radio reports TX, before the text
    postamble_s: mark tail after the text
    release_delay_s: wait after the last sample before releasing PTT; must cover the
        audio buffering between this block and the radio (about 0.4 s in the demo)
    """

    def __init__(self, samp_rate=48000, baud=45.45, mark_hz=2125.0, space_hz=2295.0,
                 amplitude=0.3, preamble_s=0.3, postamble_s=0.2, release_delay_s=0.5,
                 lib_dir=''):
        gr.sync_block.__init__(self, name='RTTY AFSK Mod', in_sig=None, out_sig=[np.float32])
        self._cfg = dict(samp_rate=float(samp_rate), baud=float(baud), mark=float(mark_hz),
                         space=float(space_hz), amp=float(amplitude), pre=float(preamble_s),
                         post=float(postamble_s), release=float(release_delay_s),
                         lib_dir=lib_dir)
        self._tx = None
        self._lock = threading.Lock()
        self.message_port_register_in(pmt.intern('text'))
        self.set_msg_handler(pmt.intern('text'), self._handle_text)
        self.message_port_register_in(pmt.intern('status'))
        self.set_msg_handler(pmt.intern('status'), self._handle_status)
        self.message_port_register_out(pmt.intern('ptt'))
        self.message_port_register_out(pmt.intern('info'))

    def start(self):
        c = self._cfg
        if c['lib_dir'] and c['lib_dir'] not in sys.path:
            sys.path.insert(0, c['lib_dir'])
        import rtty
        gen = rtty.AfskGenerator(c['samp_rate'], c['baud'], c['mark'], c['space'], c['amp'])
        self._tx = rtty.RttyTransmitter(gen, self._request_ptt, self._info, preamble_s=c['pre'],
                                        postamble_s=c['post'], release_delay_s=c['release'])
        return True

    def stop(self):
        with self._lock:
            if self._tx is not None:
                self._tx.abort()        # releases PTT if this block keyed it
        return True

    def _request_ptt(self, on):
        self.message_port_pub(pmt.intern('ptt'), pmt.cons(pmt.intern('rtty'), pmt.from_bool(bool(on))))

    def _info(self, text):
        print(text, file=sys.stderr)
        self.message_port_pub(pmt.intern('info'), pmt.intern(text))

    def _handle_text(self, msg):
        with self._lock:
            if self._tx is not None:
                self._tx.send(_text(msg))

    def _handle_status(self, msg):
        with self._lock:
            if self._tx is not None:
                self._tx.set_radio_state(_text(msg))

    def work(self, input_items, output_items):
        out = output_items[0]
        with self._lock:
            if self._tx is None:
                out[:] = 0
            else:
                out[:] = self._tx.produce(len(out))
        return len(out)
