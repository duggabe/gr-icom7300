"""RTTY (Baudot / ITA2) AFSK modulator, independent of GNU Radio.

Standard amateur RTTY: 45.45 baud, 170 Hz shift, 1 start bit, 5 data bits (LSB first),
1.5 stop bits. With the radio in LSB-D, audio mark 2125 Hz / space 2295 Hz puts mark
on the higher RF frequency, which is the normal on-air sense. For USB-D, swap them.

Classes:
    BaudotEncoder   text -> 5-bit codes, with LTRS/FIGS shift handling
    AfskGenerator   codes -> phase-continuous float audio, with ramped burst start/end
    RttyTransmitter queues text, requests PTT, waits for TX, sends, releases PTT
    RttyDemodulator float audio -> text (mark/space filter pair, UART, Baudot)
    LineAssembler   received characters -> display lines
"""

from __future__ import annotations

import math
import time
from typing import Callable, Optional

import numpy as np

LTRS, FIGS = 0x1F, 0x1B
CR, LF, SPACE = 0x08, 0x02, 0x04

# index = Baudot code. None = no character (shift codes, or symbols that differ
# between ITA2 and US-TTY, which are left out so every receiver agrees).
_LTRS_TABLE = [None, "E", "\n", "A", " ", "S", "I", "U", "\r", "D", "R", "J", "N", "F", "C", "K",
               "T", "Z", "L", "W", "H", "Y", "P", "Q", "O", "B", "G", None, "M", "X", "V", None]
_FIGS_TABLE = [None, "3", "\n", "-", " ", None, "8", "7", "\r", None, "4", None, ",", None, ":", "(",
               "5", None, ")", "2", None, "6", "0", "1", "9", "?", None, None, ".", "/", None, None]

LETTERS = {c: i for i, c in enumerate(_LTRS_TABLE) if c and c not in " \r\n"}
FIGURES = {c: i for i, c in enumerate(_FIGS_TABLE) if c and c not in " \r\n"}
COMMON = {" ": SPACE, "\r": CR, "\n": LF}


def unsupported(text: str) -> str:
    """Characters in text that RTTY can't send (they are skipped)."""
    return "".join(sorted({c for c in text.upper()
                           if c not in LETTERS and c not in FIGURES and c not in COMMON}))


def decode(codes: list[int]) -> str:
    """Baudot codes -> text (for tests and checking)."""
    out, figs = [], False
    for c in codes:
        if c == LTRS:
            figs = False
        elif c == FIGS:
            figs = True
        else:
            ch = (_FIGS_TABLE if figs else _LTRS_TABLE)[c]
            if c == SPACE:
                figs = False              # unshift-on-space receiver
            if ch:
                out.append(ch)
    return "".join(out)


class BaudotEncoder:
    """Keeps the shift state across calls within one transmission."""

    def __init__(self) -> None:
        self.shift: Optional[str] = None     # 'L', 'F' or None (unknown)

    def reset(self) -> None:
        self.shift = None

    def encode(self, text: str) -> list[int]:
        codes: list[int] = []
        for ch in text.upper():
            if ch in COMMON:
                codes.append(COMMON[ch])
                if ch == " " and self.shift == "F":
                    # receiver may or may not unshift on space: make the next shift explicit
                    self.shift = None
            elif ch in LETTERS:
                if self.shift != "L":
                    codes.append(LTRS)
                    self.shift = "L"
                codes.append(LETTERS[ch])
            elif ch in FIGURES:
                if self.shift != "F":
                    codes.append(FIGS)
                    self.shift = "F"
                codes.append(FIGURES[ch])
            # anything else is skipped
        return codes


class AfskGenerator:
    """Phase-continuous FSK. Audio is generated when queued and read out in blocks."""

    def __init__(self, samp_rate: float = 48000, baud: float = 45.45, mark_hz: float = 2125.0,
                 space_hz: float = 2295.0, amplitude: float = 0.3, stop_bits: float = 1.5,
                 ramp_s: float = 0.005) -> None:
        self.fs = float(samp_rate)
        self.baud = float(baud)
        self.mark = float(mark_hz)
        self.space = float(space_hz)
        self.amp = float(amplitude)
        self.stop_bits = float(stop_bits)
        self.ramp = max(1, int(round(ramp_s * self.fs)))
        self._phase = 0.0
        self._carry = 0.0                 # fractional samples carried between segments
        self._chunks: list[np.ndarray] = []
        self._pos = 0                     # read position in _chunks[0]

    # -- generation ----------------------------------------------------------
    def _tone(self, freq: float, seconds: float) -> np.ndarray:
        exact = seconds * self.fs + self._carry
        n = int(exact)
        self._carry = exact - n
        ph = self._phase + 2 * math.pi * freq / self.fs * np.arange(1, n + 1)
        if n:
            self._phase = float(ph[-1] % (2 * math.pi))
        return self.amp * np.cos(ph).astype(np.float32)

    def _bits(self, codes: list[int]) -> np.ndarray:
        parts = []
        bit = 1.0 / self.baud
        for code in codes:
            parts.append(self._tone(self.space, bit))                     # start bit
            for i in range(5):
                parts.append(self._tone(self.mark if (code >> i) & 1 else self.space, bit))
            parts.append(self._tone(self.mark, bit * self.stop_bits))      # stop bits
        return np.concatenate(parts) if parts else np.zeros(0, np.float32)

    def start_burst(self, preamble_s: float) -> None:
        """Mark idle with a raised-cosine ramp up, then two LTRS for receiver sync."""
        self._phase, self._carry = 0.0, 0.0
        pre = self._tone(self.mark, max(preamble_s, 2 * self.ramp / self.fs))
        r = min(self.ramp, len(pre))
        pre[:r] *= (0.5 - 0.5 * np.cos(np.pi * np.arange(r) / r)).astype(np.float32)
        self._chunks.append(pre)
        self.add_codes([LTRS, LTRS])

    def add_codes(self, codes: list[int]) -> None:
        if codes:
            self._chunks.append(self._bits(codes))

    def end_burst(self, postamble_s: float) -> None:
        """Mark tail ending in a raised-cosine ramp down to silence."""
        post = self._tone(self.mark, max(postamble_s, 2 * self.ramp / self.fs))
        r = min(self.ramp, len(post))
        post[-r:] *= (0.5 + 0.5 * np.cos(np.pi * np.arange(1, r + 1) / r)).astype(np.float32)
        self._chunks.append(post)

    # -- read-out --------------------------------------------------------------
    def pending(self) -> int:
        return sum(len(c) for c in self._chunks) - self._pos

    def read(self, n: int) -> np.ndarray:
        """Up to n queued samples (fewer if the queue runs out)."""
        out = []
        need = n
        while need and self._chunks:
            c = self._chunks[0]
            take = min(need, len(c) - self._pos)
            out.append(c[self._pos:self._pos + take])
            self._pos += take
            need -= take
            if self._pos >= len(c):
                self._chunks.pop(0)
                self._pos = 0
        return np.concatenate(out) if out else np.zeros(0, np.float32)

    def clear(self) -> None:
        self._chunks.clear()
        self._pos = 0


IDLE, WAIT_TX, SENDING, CLOSING, RELEASE = "IDLE", "WAIT_TX", "SENDING", "CLOSING", "RELEASE"


class RttyTransmitter:
    """Text in -> audio out, coordinating PTT with the CI-V block.

    request_ptt(bool) asks for TX/RX. set_radio_state(str) must be fed the PTT block's
    status ("TX", "RX", ...). Audio starts only once the radio reports TX. PTT is
    released only if this transmitter requested it (a manually held PTT stays on).
    Call produce(n) from the audio thread; it always returns n samples.
    """

    def __init__(self, gen: AfskGenerator, request_ptt: Callable[[bool], None],
                 on_info: Optional[Callable[[str], None]] = None, preamble_s: float = 0.3,
                 postamble_s: float = 0.2, release_delay_s: float = 0.5,
                 tx_wait_s: float = 3.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.gen = gen
        self.request_ptt = request_ptt
        self.on_info = on_info or (lambda s: None)
        self.preamble = preamble_s
        self.postamble = postamble_s
        self.release_delay = release_delay_s
        self.tx_wait = tx_wait_s
        self.clock = clock
        self.enc = BaudotEncoder()
        self.state = IDLE
        self.radio_tx = False
        self.we_keyed = False
        self._pending: list[str] = []
        self._deadline = 0.0

    def send(self, text: str) -> None:
        bad = unsupported(text)
        if bad:
            self.on_info(f"RTTY: skipped unsupported characters {bad!r}")
        if not text.strip():
            if self.state != IDLE:            # empty text = abort, as for CW
                self.abort()
                self.on_info("RTTY: stopped")
            return
        if self.state == SENDING:
            self.gen.add_codes(self.enc.encode(text + "\r\n"))
            return
        self._pending.append(text)
        if self.state == IDLE:
            self._begin()

    def abort(self) -> None:
        self.gen.clear()
        self._pending.clear()
        if self.we_keyed:
            self.request_ptt(False)
        self.we_keyed = False
        self.state = IDLE

    def set_radio_state(self, status: str) -> None:
        tx = status.startswith("TX")
        was = self.radio_tx
        self.radio_tx = tx
        if self.state == WAIT_TX and tx:
            self._start_burst()
        elif was and not tx and self.state in (SENDING, CLOSING, RELEASE):
            # radio left TX underneath us (button released, watchdog, error): stop sending
            if self.state != RELEASE:
                self.on_info("RTTY: transmission cut short (radio left TX)")
            self.gen.clear()
            self._pending.clear()
            self.we_keyed = False
            self.state = IDLE

    def _begin(self) -> None:
        if self.radio_tx:
            self._start_burst()
        else:
            self.we_keyed = True
            self.state = WAIT_TX
            self._deadline = self.clock() + self.tx_wait
            self.request_ptt(True)

    def _start_burst(self) -> None:
        self.enc.reset()
        self.gen.start_burst(self.preamble)
        self.gen.add_codes(self.enc.encode("\r\n" + "".join(t + "\r\n" for t in self._pending)))
        self._pending.clear()
        self.state = SENDING

    def produce(self, n: int) -> np.ndarray:
        now = self.clock()
        if self.state == WAIT_TX and now > self._deadline:
            self.on_info("RTTY: radio did not enter TX; text dropped")
            self.abort()
        out = self.gen.read(n)
        if len(out) < n:
            if self.state == SENDING:
                self.gen.end_burst(self.postamble)
                self.state = CLOSING
                out = np.concatenate([out, self.gen.read(n - len(out))])
            if self.state == CLOSING and self.gen.pending() == 0:
                if self._pending and self.radio_tx:
                    self._start_burst()
                    out = np.concatenate([out, self.gen.read(n - len(out))])
                elif self.we_keyed:
                    self.state = RELEASE
                    self._deadline = now + self.release_delay
                else:
                    self.state = IDLE
        if self.state == RELEASE:
            if self._pending and self.radio_tx:
                self._start_burst()
            elif now >= self._deadline:
                self.request_ptt(False)
                self.we_keyed = False
                self.state = IDLE
                if self._pending:
                    self._begin()
        if len(out) < n:
            out = np.concatenate([out, np.zeros(n - len(out), np.float32)])
        return out


# -- receive -------------------------------------------------------------------

# Receive also shows the US-TTY figures that transmit leaves out (most amateur RTTY uses
# US-TTY). Code 5 (bell) is dropped.
_RX_FIGS_TABLE = list(_FIGS_TABLE)
for _code, _ch in {9: "$", 11: "'", 13: "!", 17: '"', 20: "#", 26: "&", 30: ";"}.items():
    _RX_FIGS_TABLE[_code] = _ch


def _lowpass(cutoff: float, fs: float, ntaps: int) -> np.ndarray:
    """Windowed-sinc (Blackman) low-pass taps with unity DC gain."""
    n = np.arange(ntaps) - (ntaps - 1) / 2
    h = np.sinc(2 * cutoff / fs * n) * np.blackman(ntaps)
    return h / h.sum()


class RttyDemodulator:
    """Non-coherent FSK receiver: audio -> characters.

    1. Mix the audio down by the centre frequency, low-pass (keeps about +-300 Hz) and
       decimate to about 2 kHz.
    2. Mark and space detectors: mix each tone to DC and average over one bit (the
       matched filter for a bit), giving magnitudes m and s.
    3. d = (m - s) / (m + s), from +1 (mark) to -1 (space); independent of signal level.
    4. UART: a mark-to-space crossing starts a character; bits are sampled one bit apart
       at the detector peaks; a character needs a space start bit and a mark stop bit.
    5. Baudot: LTRS/FIGS shift, optional unshift on space.

    squelch: minimum mean |d| over a character (0 = off). Clean signals give about 0.9,
        noise about 0.4, so the default 0.6 suppresses most noise garbage.
    reverse: swap mark and space (signal inverted, or radio in the other sideband).
    """

    def __init__(self, samp_rate: float = 48000, baud: float = 45.45, mark_hz: float = 2125.0,
                 space_hz: float = 2295.0, reverse: bool = False, squelch: float = 0.6,
                 usos: bool = True) -> None:
        self.fs = float(samp_rate)
        self.baud = float(baud)
        self.mark = float(mark_hz)
        self.space = float(space_hz)
        self.reverse = bool(reverse)
        self.squelch = float(squelch)
        self.usos = bool(usos)
        self.fc = (self.mark + self.space) / 2
        self.decim = max(1, int(round(self.fs / 2000.0)))
        self.fs2 = self.fs / self.decim
        self.taps = _lowpass(300.0, self.fs, 511)[::-1].astype(np.complex64)
        self.spb = self.fs2 / self.baud                       # samples per bit after decimation
        self.box = max(1, int(round(self.spb)))
        self.reset()

    def reset(self) -> None:
        """Clear filter and UART state (e.g. after transmitting)."""
        self._n1 = 0                       # input sample counter (mixer phase)
        self._n2 = 0                       # decimated sample counter (tone mixers)
        self._buf = np.zeros(len(self.taps) - 1, np.complex64)
        self._tail_m = np.zeros(self.box - 1, np.complex64)
        self._tail_s = np.zeros(self.box - 1, np.complex64)
        self._t = 0                        # UART sample index
        self._prev = 0.0
        self._bits: list[float] = []
        self._next = 0.0
        self._sampling = False
        self.figs = False

    def detector(self, audio: np.ndarray) -> np.ndarray:
        """Audio -> mark/space decision values d at fs2 (mark positive)."""
        x = np.asarray(audio, np.float64)
        n = self._n1 + np.arange(len(x))
        self._n1 += len(x)
        z = (x * np.exp(-2j * np.pi * self.fc / self.fs * n)).astype(np.complex64)
        buf = np.concatenate([self._buf, z])
        N = len(self.taps)
        if len(buf) < N:
            self._buf = buf
            return np.zeros(0)
        nout = (len(buf) - N) // self.decim + 1
        win = np.lib.stride_tricks.sliding_window_view(buf, N)[:(nout - 1) * self.decim + 1:self.decim]
        y = win @ self.taps
        self._buf = buf[nout * self.decim:]

        k = self._n2 + np.arange(len(y))
        self._n2 += len(y)
        mags = []
        for f, attr in ((self.mark - self.fc, "_tail_m"), (self.space - self.fc, "_tail_s")):
            t = y * np.exp(-2j * np.pi * f / self.fs2 * k).astype(np.complex64)
            full = np.concatenate([getattr(self, attr), t])
            c = np.cumsum(np.concatenate([[0], full]))
            mags.append(np.abs(c[self.box:] - c[:-self.box]))
            setattr(self, attr, full[len(full) - (self.box - 1):] if self.box > 1 else full[:0])
        m, s = mags
        d = (m - s) / (m + s + 1e-12)
        return -d if self.reverse else d

    def process(self, audio: np.ndarray) -> list[int]:
        """Audio -> list of received Baudot codes."""
        codes = []
        for v in self.detector(audio):
            v = float(v)
            if self._sampling:
                if self._t >= self._next - 0.5:
                    self._bits.append(v)
                    self._next += self.spb
                    if len(self._bits) == 7:
                        self._sampling = False
                        b = self._bits
                        quality = sum(abs(x) for x in b) / 7
                        if b[0] < 0 and b[6] > 0 and quality >= self.squelch:
                            codes.append(sum(1 << i for i in range(5) if b[1 + i] > 0))
            elif self._prev > 0 >= v:
                tc = self._t - 1 + self._prev / (self._prev - v)    # interpolated crossing
                self._next = tc + self.spb / 2
                self._bits = []
                self._sampling = True
            self._prev = v
            self._t += 1
        return codes

    def decode(self, codes: list[int]) -> str:
        """Baudot codes -> text, keeping the shift state between calls."""
        out = []
        for c in codes:
            if c == LTRS:
                self.figs = False
            elif c == FIGS:
                self.figs = True
            else:
                ch = (_RX_FIGS_TABLE if self.figs else _LTRS_TABLE)[c]
                if c == SPACE and self.usos:
                    self.figs = False
                if ch:
                    out.append(ch)
        return "".join(out)

    def receive(self, audio: np.ndarray) -> str:
        return self.decode(self.process(audio))


class LineAssembler:
    """Characters -> display lines: split at LF, wrap at width (at a space if possible),
    and flush a partial line after idle_s without new characters. CR is dropped."""

    def __init__(self, width: int = 64, idle_s: float = 2.0) -> None:
        self.width = int(width)
        self.idle = float(idle_s)
        self._line = ""
        self._last = 0.0

    def feed(self, text: str, now: float) -> list[str]:
        lines = []
        for ch in text:
            if ch == "\r":
                continue
            if ch == "\n":
                if self._line.strip():
                    lines.append(self._line.rstrip())
                self._line = ""
                continue
            self._line += ch
            if len(self._line) > self.width:
                cut = self._line.rfind(" ", 0, self.width + 1)
                cut = cut if cut > 0 else self.width
                lines.append(self._line[:cut].rstrip())
                self._line = self._line[cut:].lstrip()
        if text:
            self._last = now
        return lines

    def poll(self, now: float) -> list[str]:
        """Partial line, once nothing has arrived for idle_s."""
        if self._line.strip() and now - self._last >= self.idle:
            line, self._line = self._line.rstrip(), ""
            return [line]
        return []
