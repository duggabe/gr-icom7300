"""T/R sequencing on top of icom_civ, independent of GNU Radio.

States:
    RX    receiving; TX audio muted
    LEAD  radio keyed and confirmed, waiting `lead` s before un-muting audio
    TX    transmitting; audio un-muted; watchdog running
    TAIL  PTT released; audio muted, carrier held `tail` s so buffered audio drains
    CW    radio's internal keyer is sending (CW mode + break-in; no PTT used)

All radio I/O runs on one worker thread, so callers (GNU Radio message handlers,
GUI callbacks) never block on the serial port. Callbacks run on that worker thread.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Callable, Optional

import icom_civ
from icom_civ import CivError, CivNak

RX, LEAD, TX, TAIL, CW = "RX", "LEAD", "TX", "TAIL", "CW"

CW_POLL_INTERVAL = 0.2      # after the estimated end of CW, poll TX state this often
CW_POLL_LIMIT = 25          # ...for at most this many polls (5 s) before declaring RX

_FAIL = object()            # returned by _radio() when a radio call failed


class TxController:
    def __init__(
        self,
        civ: icom_civ.CivPort,
        lead: float = 0.05,
        tail: float = 0.05,
        tx_timeout: float = 180.0,
        cw_wpm: int = 0,
        usb_mod_level: float = -1,
        on_state: Optional[Callable[[str], None]] = None,
        on_mute: Optional[Callable[[bool], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        on_info: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.civ = civ
        self.lead = lead
        self.tail = tail
        self.tx_timeout = tx_timeout
        self.on_state = on_state or (lambda s: None)
        self.on_mute = on_mute or (lambda m: None)
        self.on_error = on_error or (lambda e: None)
        self.on_info = on_info or (lambda t: None)
        self._wpm = cw_wpm          # 0 = use the radio's current keyer speed
        self._wpm_est = cw_wpm or 20  # speed used to estimate CW duration
        self._usbmod_want: Optional[float] = usb_mod_level if usb_mod_level >= 0 else None
        self._usbmod_applied: Optional[float] = None
        self._q: "queue.Queue[tuple]" = queue.Queue()
        self._state = RX
        self._deadline: Optional[float] = None
        self._tx_start = 0.0
        self._cw_end = 0.0
        self._cw_polls = 0
        self._thread: Optional[threading.Thread] = None

    # -- public API (thread-safe, non-blocking) ----------------------------
    @property
    def state(self) -> str:
        return self._state

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="tx-controller", daemon=True)
        self._thread.start()

    def ptt(self, on: bool) -> None:
        self._q.put(("ptt", bool(on)))

    def cw(self, text: str) -> None:
        """Queue CW text for the radio's keyer; empty text aborts sending."""
        self._q.put(("cw", text))

    def set_wpm(self, wpm: int) -> None:
        self._q.put(("wpm", int(wpm)))

    def set_usb_mod_level(self, pct: float) -> None:
        """Set the radio's USB MOD Level (0-100 %). Rapid calls (a slider being
        dragged) collapse into one radio write of the latest value."""
        self._usbmod_want = float(pct)
        self._q.put(("usbmod",))

    def setup_cw_decode(self, filt: int = 3) -> None:
        """Prepare the radio's own CW decoder: CW mode (CW-R is kept) with filter `filt`
        (default FIL3, the narrowest, against adjacent stations) and the MENU screen's
        [KEYER/DECODE] key set to open CW DECODE. Ignored while transmitting."""
        if filt not in (1, 2, 3):
            raise ValueError("filter must be 1, 2 or 3")
        self._q.put(("cwdecode", filt))

    def setup_data_mode(self, sideband: str = "LSB", filt: int = 2, pbt: Optional[int] = 255) -> None:
        """Switch to LSB-D or USB-D (DATA on) with filter `filt`, for AFSK such as RTTY, and
        set both Twin PBT controls to `pbt` (None leaves them alone). The defaults suit
        2125/2295 Hz AFSK: SSB-D FIL2 is 1.2 kHz wide and centred near 1500 Hz, so 2295 Hz
        is cut; PBT at the top of its range (the radio reads back 250) moves it to about
        1350-2750 Hz, measured on an IC-7300MK2. Ignored while transmitting."""
        if filt not in (1, 2, 3):
            raise ValueError("filter must be 1, 2 or 3")
        if pbt is not None and not 0 <= pbt <= 255:
            raise ValueError("PBT must be 0-255")
        self._q.put(("datamode", sideband.upper(), filt, pbt))

    def close(self, timeout: float = 2.0) -> None:
        """Unkey and stop the worker. Safe to call more than once."""
        if self._thread is not None:
            self._q.put(("shutdown",))
            self._thread.join(timeout)
            self._thread = None

    # -- worker ------------------------------------------------------------
    def _run(self) -> None:
        self.on_mute(True)
        self.on_state(RX)
        if self._wpm:
            self._radio(self.civ.set_cw_speed, self._wpm)
        if self._usbmod_want is not None:
            self._on_usbmod()
        try:
            while True:
                wait = None if self._deadline is None else max(0.0, self._deadline - time.monotonic())
                try:
                    ev = self._q.get(timeout=wait)
                except queue.Empty:
                    self._deadline = None
                    self._on_timer()
                    continue
                if ev[0] == "shutdown":
                    break
                getattr(self, "_on_" + ev[0])(*ev[1:])
        finally:
            self._safe_unkey()

    def _set_state(self, s: str) -> None:
        if s != self._state:
            self._state = s
            self.on_state(s)

    def _after(self, seconds: float) -> None:
        self._deadline = time.monotonic() + max(0.0, seconds)

    def _radio(self, fn, *args):
        """Run one radio call; on failure report, force safe RX, and return _FAIL."""
        try:
            return fn(*args)
        except (CivError, ValueError) as e:
            self.on_error(str(e))
            self._safe_unkey()
            return _FAIL

    def _safe_unkey(self) -> None:
        self.on_mute(True)
        self._deadline = None
        try:
            if self._state == CW:
                self.civ.stop_cw()
            self.civ.set_ptt(False)
        except CivError as e:
            self.on_error(f"unkey failed: {e}")
        self._state = RX
        self.on_state(RX)

    # -- events --------------------------------------------------------------
    def _on_ptt(self, on: bool) -> None:
        s = self._state
        if on:
            if s == RX:
                if self._radio(self._key) is _FAIL:
                    return
                self._set_state(LEAD)
                self._after(self.lead)
            elif s == TAIL:                     # re-pressed during tail: resume
                self._set_state(TX)
                self.on_mute(False)
                self._deadline = self._tx_start + self.tx_timeout
            elif s == CW:
                self.on_error("PTT ignored: CW in progress")
        else:
            if s == LEAD:
                self._safe_unkey()
            elif s == TX:
                self.on_mute(True)
                self._set_state(TAIL)
                self._after(self.tail)

    def _key(self) -> bool:
        self.civ.set_ptt(True)
        if not self.civ.get_ptt():
            raise CivError("radio did not enter TX")
        return True

    def _on_cw(self, text: str) -> None:
        if not text.strip():
            if self._state == CW:
                self._safe_unkey()              # stop_cw + unkey
            return
        if self._state not in (RX, CW):
            self.on_error("CW ignored: PTT transmit in progress")
            return
        try:
            secs = icom_civ.cw_duration(text, self._wpm_est)
        except ValueError as e:
            self.on_error(str(e))
            return
        if secs > self.tx_timeout:
            self.on_error(f"CW text too long ({secs:.0f}s > {self.tx_timeout:.0f}s TX timeout)")
            return
        if self._radio(self.civ.send_cw, text) is _FAIL:
            return
        now = time.monotonic()
        self._cw_end = max(now, self._cw_end if self._state == CW else now) + secs
        self._cw_polls = 0
        self._set_state(CW)
        self._deadline = self._cw_end

    def _on_wpm(self, wpm: int) -> None:
        self._wpm = wpm
        if wpm:
            self._wpm_est = min(max(wpm, icom_civ.CW_WPM_MIN), icom_civ.CW_WPM_MAX)
            self._radio(self.civ.set_cw_speed, wpm)

    def _on_usbmod(self) -> None:
        want = self._usbmod_want
        if want is None or want == self._usbmod_applied:
            return
        try:
            self.civ.set_usb_mod_level(want)
            self._usbmod_applied = want
        except CivNak:
            # radio refused the setting: report it, but don't drop an ongoing TX for it
            self.on_error(f"radio rejected USB MOD Level {want:g}%")
        except CivError as e:
            self.on_error(str(e))
            self._safe_unkey()

    def _on_cwdecode(self, want_filt: int) -> None:
        if self._state != RX:
            self.on_error("radio setup ignored: transmitting")
            return
        try:
            old_mode, old_filt = self.civ.get_mode()
            mode = old_mode if old_mode in ("CW", "CW-R") else "CW"
            filt = want_filt
            if (mode, filt) != (old_mode, old_filt):
                self.civ.set_mode(mode, filt=filt)
            self.civ.set_keyer_decode_key("DECODE")
        except CivError as e:
            self.on_error(f"CW decode setup failed: {e}")
            return
        self.on_info(f"Radio: {mode} mode, FIL{filt}. "
                     "Touch MENU, then KEYER/DECODE, for the radio's CW DECODE screen.")

    def _on_datamode(self, sideband: str, filt: int, pbt: Optional[int]) -> None:
        if self._state != RX:
            self.on_error("radio setup ignored: transmitting")
            return
        try:
            self.civ.set_mode(sideband, data=True, filt=filt)
            if pbt is not None:
                self.civ.set_twin_pbt(pbt, pbt)
        except (CivError, ValueError) as e:
            self.on_error(f"{sideband}-D setup failed: {e}")
            return
        self.on_info(f"Radio: {sideband}-D mode, FIL{filt}"
                     + (", passband shifted up (Twin PBT)." if pbt is not None and pbt > 128 else "."))

    def _on_timer(self) -> None:
        s = self._state
        if s == LEAD:
            self._tx_start = time.monotonic()
            self._set_state(TX)
            self.on_mute(False)
            self._deadline = self._tx_start + self.tx_timeout
        elif s == TX:
            self.on_error(f"TX timeout after {self.tx_timeout:.0f}s: unkeyed")
            self._safe_unkey()
        elif s == TAIL:
            self._radio(self.civ.set_ptt, False)
            self._set_state(RX)
        elif s == CW:
            # estimated end reached: wait until the radio's break-in drops back to RX
            tx = self._radio(self.civ.get_ptt)
            if tx is _FAIL:
                return
            if tx and self._cw_polls < CW_POLL_LIMIT:
                self._cw_polls += 1
                self._after(CW_POLL_INTERVAL)
            else:
                if tx:
                    self.on_error("radio still in TX after CW: stopping")
                    self._safe_unkey()
                else:
                    self._set_state(RX)
