"""
Icom CI-V T/R control (Embedded Python Block)

Message inputs:
  ptt  - bool/int, or a (name . value) pair as sent by QT GUI Toggle Button.
         True = transmit, False = receive.
  cw   - text for the radio's CW keyer (string, or pair from QT GUI Message Edit Box).
         Empty text aborts sending. Radio must be in CW mode with break-in on.
Message outputs:
  status - symbol: RX, LEAD, TX, TAIL, CW, or "ERROR: ..."
  mute   - bool for a Mute block's set_mute port: True except while TX audio is wanted.

Needs icom_civ.py and civ_controller.py next to the .grc file (or set lib_dir).
The serial port is opened in start(), not when GRC loads the block.
"""

import sys

import pmt
from gnuradio import gr


def _value(msg):
    v = pmt.to_python(msg)
    if isinstance(v, tuple) and len(v) == 2:     # (name . value) pair
        v = v[1]
    return v


def _to_bool(v):
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "on", "tx", "pressed")
    return bool(v)


class blk(gr.basic_block):
    """Icom CI-V T/R control: PTT with lead/tail timing, TX watchdog, CW keyer.

    port: serial device, e.g. /dev/serial/by-id/usb-Icom_Inc._IC-7300MK2_...-if00
    radio_addr: CI-V address (IC-7300MK2 default 0xB6, IC-7300 0x94)
    lead_ms: delay after confirmed TX before audio is un-muted
    tail_ms: carrier hold after PTT release
    tx_timeout_s: watchdog, unkeys after this long in TX
    cw_wpm: keyer speed set on start and when changed (0 = leave radio setting)
    usb_mod_level: radio USB MOD Level %, set on start and when changed (-1 = leave)
    lib_dir: directory holding icom_civ.py (blank = flowgraph directory)
    """

    def __init__(self, port='/dev/ttyACM0', baud=115200, radio_addr=0xB6,
                 lead_ms=50, tail_ms=50, tx_timeout_s=180, cw_wpm=0, usb_mod_level=-1,
                 lib_dir=''):
        gr.basic_block.__init__(self, name='Icom CI-V PTT', in_sig=None, out_sig=None)
        self._cfg = dict(port=port, baud=int(baud), radio_addr=int(radio_addr),
                         lead=lead_ms / 1000.0, tail=tail_ms / 1000.0,
                         tx_timeout=float(tx_timeout_s), lib_dir=lib_dir)
        self._wpm = int(cw_wpm)
        self._usbmod = float(usb_mod_level)
        self._civ = None
        self._ctl = None

        self.message_port_register_in(pmt.intern('ptt'))
        self.set_msg_handler(pmt.intern('ptt'), self._handle_ptt)
        self.message_port_register_in(pmt.intern('cw'))
        self.set_msg_handler(pmt.intern('cw'), self._handle_cw)
        self.message_port_register_out(pmt.intern('status'))
        self.message_port_register_out(pmt.intern('mute'))

    # GRC turns a property with a setter into a run-time callback (e.g. a QT GUI Range)
    @property
    def cw_wpm(self):
        return self._wpm

    @cw_wpm.setter
    def cw_wpm(self, wpm):
        self._wpm = int(wpm)
        if self._ctl is not None:
            self._ctl.set_wpm(self._wpm)

    @property
    def usb_mod_level(self):
        return self._usbmod

    @usb_mod_level.setter
    def usb_mod_level(self, pct):
        self._usbmod = float(pct)
        if self._ctl is not None and self._usbmod >= 0:
            self._ctl.set_usb_mod_level(self._usbmod)

    def start(self):
        cfg = self._cfg
        if cfg['lib_dir'] and cfg['lib_dir'] not in sys.path:
            sys.path.insert(0, cfg['lib_dir'])
        import icom_civ
        import civ_controller
        self._civ = icom_civ.CivPort(cfg['port'], cfg['baud'], cfg['radio_addr'])
        try:
            self._civ.open()
        except Exception as e:
            self._civ = None
            self._publish_status(f"ERROR: cannot open {cfg['port']}: {e}")
            raise
        self._ctl = civ_controller.TxController(
            self._civ, lead=cfg['lead'], tail=cfg['tail'], tx_timeout=cfg['tx_timeout'],
            cw_wpm=self._wpm, usb_mod_level=self._usbmod, on_state=self._publish_status,
            on_mute=self._publish_mute, on_error=lambda e: self._publish_status('ERROR: ' + e))
        self._ctl.start()
        return True

    def stop(self):
        if self._ctl is not None:
            self._ctl.close()           # always unkeys
            self._ctl = None
        if self._civ is not None:
            self._civ.close()
            self._civ = None
        return True

    def _handle_ptt(self, msg):
        if self._ctl is not None:
            self._ctl.ptt(_to_bool(_value(msg)))

    def _handle_cw(self, msg):
        if self._ctl is not None:
            v = _value(msg)
            self._ctl.cw(v if isinstance(v, str) else str(v))

    def _publish_status(self, text):
        self.message_port_pub(pmt.intern('status'), pmt.intern(text))

    def _publish_mute(self, mute):
        self.message_port_pub(pmt.intern('mute'), pmt.from_bool(bool(mute)))
