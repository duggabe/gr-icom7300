"""CW (Morse) receiver: audio -> text, independent of GNU Radio.

Pipeline:
  1. Mix the audio down by the CW pitch, low-pass (+-300 Hz), decimate to 1 kHz.
  2. AFC: every 0.25 s, find the strongest tone within afc_hz of the pitch and follow it.
  3. Narrow filter (+-50 Hz) around that tone; envelope = magnitude.
  4. Keying detector: a key-down level (fast attack, ~5 s decay) and a gap level are
     tracked; key down above their midpoint (with hysteresis). Squelch: over the last
     2 s of envelope the 99th/20th percentile ratio must exceed `squelch` (noise alone
     gives 3.8-5.8; 20 ms of key-down in the window is enough to raise it, so the first
     element of a new transmission is caught). Once open it stays open for 2 s, and as
     long as the ratio stays above 0.85 x `squelch`. Changes shorter than a quarter dot
     are ignored.
  5. Timing: the dot length is fitted to the recent marks as dots (1x) or dashes (3x),
     with a capped error per mark so interference blips barely count; marks shorter
     than 0.4 dot are dropped. So the decoder follows the sender's speed (5-60 WPM). A gap of 2 dots ends a
     character; a gap 1.5 times the sender's usual letter gap (at least 4.5 dots) ends
     a word, which also handles Farnsworth spacing.
  6. Morse table, with the common prosigns shown as <AR>, <SK>, <KN>, <BK>, <HH>.

Radio: CW mode; set pitch_hz to the radio's CW Pitch (default 600 Hz).
"""

from __future__ import annotations

import numpy as np

from icom_civ import MORSE

DECODE = {code: ch for ch, code in MORSE.items()}
DECODE.update({".-.-.": "<AR>", "...-.-": "<SK>", "-.--.": "<KN>", "-...-.-": "<BK>",
               "........": "<HH>"})


def _lowpass(cutoff: float, fs: float, ntaps: int) -> np.ndarray:
    n = np.arange(ntaps) - (ntaps - 1) / 2
    h = np.sinc(2 * cutoff / fs * n) * np.blackman(ntaps)
    return h / h.sum()


class _Fir:
    """Streaming complex FIR with optional decimation."""

    def __init__(self, taps: np.ndarray, decim: int = 1) -> None:
        self.taps = taps[::-1].astype(np.complex64)
        self.decim = decim
        self.buf = np.zeros(len(taps) - 1, np.complex64)

    def __call__(self, z: np.ndarray) -> np.ndarray:
        buf = np.concatenate([self.buf, z.astype(np.complex64)])
        N = len(self.taps)
        if len(buf) < N:
            self.buf = buf
            return np.zeros(0, np.complex64)
        nout = (len(buf) - N) // self.decim + 1
        win = np.lib.stride_tricks.sliding_window_view(buf, N)[:(nout - 1) * self.decim + 1:self.decim]
        self.buf = buf[nout * self.decim:]
        return win @ self.taps


class CwDecoder:
    """Audio -> text. Call receive(audio) repeatedly; it returns new characters.

    pitch_hz: expected tone (radio CW Pitch)
    afc_hz: follow the strongest tone within this distance of pitch_hz (0 = fixed)
    wpm: starting speed guess; the decoder adapts to the sender (5-60 WPM)
    squelch: minimum ratio of key-down to gap envelope (99th/20th percentile) to decode;
        noise alone gives 3.8-5.8, so the default 6.5 keeps noise from printing
    """

    RATE = 1000.0               # envelope rate, Hz (1 ms resolution)

    def __init__(self, samp_rate: float = 48000, pitch_hz: float = 600.0, afc_hz: float = 150.0,
                 wpm: float = 20.0, squelch: float = 6.5) -> None:
        self.fs = float(samp_rate)
        self.pitch = float(pitch_hz)
        self.afc_hz = float(afc_hz)
        self.start_wpm = float(wpm)
        self.squelch = float(squelch)
        self.decim = max(1, int(round(self.fs / self.RATE)))
        self.rate = self.fs / self.decim
        self.reset()

    # -- state -----------------------------------------------------------------
    def reset(self) -> None:
        self._n1 = 0
        self._n2 = 0
        self._wide = _Fir(_lowpass(300.0, self.fs, 511), self.decim)
        self._narrow = _Fir(_lowpass(50.0, self.rate, 101))
        self._hist = np.zeros(0, np.complex64)      # recent wide samples for AFC
        self._since_afc = 0
        self.offset = 0.0                           # AFC offset from pitch, Hz
        self._phase2 = 0.0
        self.sig = 0.0                              # key-down envelope level
        self.noise = 1e-9                           # gap envelope level
        self.ratio = 0.0                            # 99th / 20th percentile
        self._ehist = np.zeros(0)                   # last 2 s of envelope
        self._lvl_n = 0
        self._open_until = 0
        self._was_open = False
        self.key = False
        self._cand = False
        self._cand_n = 0
        self._run = 0                               # samples in the current key state
        self._code = ""
        self._gap_done = 2                          # 0 none, 1 char ended, 2 word ended
        self.dot = 1200.0 / self.start_wpm          # ms
        self._marks: list[float] = []               # recent mark lengths, ms
        self._gaps: list[float] = []                # recent letter/word gaps, ms

    def interrupt(self) -> None:
        """Drop the character in progress (e.g. after transmitting), but keep the speed,
        level and squelch history so the other station's reply decodes at once."""
        self.key = False
        self._cand = False
        self._cand_n = 0
        self._run = 0
        self._code = ""
        self._gap_done = 2
        self._wide.buf[:] = 0              # the audio jumps here: no stale samples
        self._narrow.buf[:] = 0

    @property
    def wpm(self) -> float:
        return 1200.0 / self.dot

    @property
    def tone_hz(self) -> float:
        return self.pitch + self.offset

    # -- signal processing -------------------------------------------------------
    def envelope(self, audio: np.ndarray) -> np.ndarray:
        x = np.asarray(audio, np.float64)
        n = self._n1 + np.arange(len(x))
        self._n1 += len(x)
        y = self._wide(x * np.exp(-2j * np.pi * self.pitch / self.fs * n))
        parts = []
        step = int(self.rate / 4)
        for i in range(0, len(y), step):           # AFC updates every 0.25 s of audio
            seg = y[i:i + step]
            self._afc(seg)
            ph = self._phase2 + 2 * np.pi * self.offset / self.rate * np.arange(1, len(seg) + 1)
            self._phase2 = float(ph[-1] % (2 * np.pi))
            parts.append(self._narrow(seg * np.exp(-1j * ph)))
        return np.abs(np.concatenate(parts)) if parts else np.zeros(0)

    def _afc(self, y: np.ndarray) -> None:
        if self.afc_hz <= 0:
            return
        self._hist = np.concatenate([self._hist, y])[-512:]
        self._since_afc += len(y)
        if self._since_afc < self.rate / 4 or len(self._hist) < 512:
            return
        self._since_afc = 0
        spec = np.abs(np.fft.fftshift(np.fft.fft(self._hist * np.hanning(512)))) ** 2
        freqs = np.fft.fftshift(np.fft.fftfreq(512, 1 / self.rate))
        band = np.abs(freqs) <= self.afc_hz
        k = np.argmax(np.where(band, spec, 0))
        if spec[k] > 20 * np.median(spec[band]):                    # a clear tone
            f = freqs[k]
            if 0 < k < 511:                                         # parabolic peak
                a, b, c = np.log(spec[k - 1:k + 2] + 1e-30)
                f += 0.5 * (a - c) / (a - 2 * b + c) * (freqs[1] - freqs[0])
            step = f - self.offset
            self.offset += step if abs(step) > 25 else 0.5 * step

    # -- keying and timing ---------------------------------------------------------
    def process(self, audio: np.ndarray) -> str:
        out = []
        ms = 1000.0 / self.rate
        env = self.envelope(audio)
        win = int(2 * self.rate)
        comb = np.concatenate([self._ehist, env])
        h = len(self._ehist)
        self._ehist = comb[-win:]
        for i, e in enumerate(env):
            if self._lvl_n % 50 == 0:                               # levels every 50 ms
                w = comb[max(0, h + i + 1 - win):h + i + 1]
                if len(w) >= win:                                  # full 2 s only
                    p20, p99 = np.percentile(w, [20, 99])
                    self.ratio = p99 / (p20 + 1e-12)                # squelch statistic
            self._lvl_n += 1
            # threshold levels: key-down level with fast attack, gap level from gap samples
            if e > self.sig:
                self.sig += 0.3 * (e - self.sig)
            elif self.key:
                self.sig += 0.01 * (e - self.sig)
            else:
                # slow decay (~5 s): outlasts the squelch window when a station stops
                self.sig += 0.0002 * (self.noise - self.sig)
            if not self.key and e < self.noise + 0.3 * (self.sig - self.noise):
                self.noise += 0.005 * (e - self.noise)              # ~0.2 s
            if self.ratio > self.squelch:
                self._open_until = self._lvl_n + 2 * self.rate              # hold 2 s
            open_ = self._lvl_n < self._open_until or (
                self._open_until > 0 and self.ratio > 0.85 * self.squelch and self._was_open)
            self._was_open = open_
            span = self.sig - self.noise
            if self.key:
                want = open_ and e > self.noise + 0.4 * span
            else:
                want = open_ and e > self.noise + 0.6 * span
            # debounced state change
            debounce = max(3, int(0.25 * self.dot / ms))
            if want != self.key:
                if want == self._cand:
                    self._cand_n += 1
                else:
                    self._cand, self._cand_n = want, 1
                if self._cand_n >= debounce:
                    self._edge(want, (self._run - self._cand_n + 1) * ms, out)
                    self.key = want
                    self._run = self._cand_n
                    continue
            else:
                self._cand_n = 0
            self._run += 1
            if not self.key:
                self._gap((self._run) * ms, out)
        return "".join(out)

    def _edge(self, key_down: bool, dur_ms: float, out: list[str]) -> None:
        if key_down:                        # a space just ended
            self._gap(dur_ms, out)
            if dur_ms >= 2 * self.dot and dur_ms < 20 * self.dot:
                self._gaps = (self._gaps + [dur_ms])[-15:]
            return
        # a mark just ended
        if dur_ms > 10 * self.dot:          # carrier / tune-up: ignore
            self._code = ""
            return
        if dur_ms < 0.4 * self.dot and len(self._marks) >= 6:
            return                          # blip (interference, noise): not an element
        self._marks = (self._marks + [dur_ms])[-24:]
        self._update_speed()
        self._code += "." if dur_ms < 2 * self.dot else "-"
        self._gap_done = 0
        if len(self._code) > 8:
            self._code = ""

    _DOT_GRID = 1200.0 / np.geomspace(60, 5, 200)                  # candidate dots, ms

    def _update_speed(self) -> None:
        """Fit the dot length d that best explains the recent marks as dots (d) or dashes
        (3d). Each mark's error |log(mark / nearest)| is capped, so blips and other
        stray marks barely matter; a small pull toward the current estimate breaks ties
        (a run of dots alone fits d and d/3 equally well)."""
        if len(self._marks) < 4:
            return
        m = np.log(np.array(self._marks))[:, None]
        g = np.log(self._DOT_GRID)[None, :]
        err = np.minimum(np.abs(m - g), np.abs(m - g - np.log(3)))
        loss = np.minimum(err, 0.5).sum(0) + 0.5 * np.abs(g[0] - np.log(self.dot))
        new = float(self._DOT_GRID[int(np.argmin(loss))])
        w = 1.0 if abs(new / self.dot - 1) > 0.3 else 0.3          # big change: snap
        self.dot = min(max((1 - w) * self.dot + w * new, 1200 / 60), 1200 / 5)

    def _word_gap(self) -> float:
        """Letter gaps are the lower cluster of recent gaps (word gaps and pauses the
        upper one); a word gap is 1.5 times the median letter gap."""
        g = sorted(self._gaps)
        if len(g) < 5:
            return 5 * self.dot
        ratios = [g[i + 1] / g[i] for i in range(len(g) - 1)]
        i = int(np.argmax(ratios))
        letters = g[:i + 1] if ratios[i] > 1.6 else g
        return max(4.5 * self.dot, 1.5 * float(np.median(letters)))

    def _gap(self, dur_ms: float, out: list[str]) -> None:
        if self._gap_done < 1 and dur_ms >= 2 * self.dot:
            out.append(DECODE.get(self._code, "*") if self._code else "")
            self._code = ""
            self._gap_done = 1
        if self._gap_done < 2 and dur_ms >= self._word_gap():
            out.append(" ")
            self._gap_done = 2

    receive = process
