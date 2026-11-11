"""CW decoder: keyed-tone simulator -> CwDecoder, across speed, pitch, noise and fading."""

import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import cw_rx  # noqa: E402
from icom_civ import MORSE  # noqa: E402

FS = 48000
TEXT = "CQ CQ DE N0CALL N0CALL PSE K"
TAIL = "DE N0CALL N0CALL PSE K"      # the first word may be lost while the speed locks on


def keyed(text, wpm=20, pitch=600.0, noise=0.0, seed=1, amp=0.3, lead=3.0, char_wpm=None,
          jitter=0.0):
    """Morse audio with 5 ms keying edges. char_wpm > wpm gives Farnsworth spacing.
    '^' joins the next two characters without a letter gap (prosign)."""
    dot, cdot = 1.2 / wpm, 1.2 / (char_wpm or wpm)
    rng = np.random.default_rng(seed)
    segs = [(0, lead)]
    for w, word in enumerate(text.split()):
        if w:
            segs.append((0, 7 * dot))
        chars, joined = [], []            # joined[i]: no letter gap before chars[i]
        arm = 0
        for ch in word:
            if ch == "^":
                arm = 2
                continue
            chars.append(ch)
            joined.append(arm == 1)
            arm = max(0, arm - 1)
        for i, ch in enumerate(chars):
            if i:
                segs.append((0, cdot if joined[i] else 3 * dot))
            for j, el in enumerate(MORSE[ch]):
                if j:
                    segs.append((0, cdot * (1 + jitter * rng.standard_normal())))
                segs.append((1, (cdot if el == "." else 3 * cdot) * (1 + jitter * rng.standard_normal())))
    segs.append((0, lead))
    env = np.concatenate([np.full(int(d * FS), k, float) for k, d in segs])
    r = int(0.005 * FS)
    w = np.hanning(2 * r)
    env = np.convolve(env, w / w.sum(), "same")
    t = np.arange(len(env)) / FS
    return amp * env * np.cos(2 * np.pi * pitch * t) + noise * rng.standard_normal(len(env))


def decode(audio, **kw):
    return cw_rx.CwDecoder(**kw).receive(audio).strip()


class TestCwDecoder(unittest.TestCase):
    def test_speeds(self):
        for wpm in (13, 20):
            self.assertEqual(decode(keyed(TEXT, wpm)), TEXT)
        for wpm in (8, 30, 40, 50):                      # far from the 20 WPM start guess
            d = cw_rx.CwDecoder()
            self.assertTrue(d.receive(keyed(TEXT, wpm)).strip().endswith(TAIL), wpm)
            self.assertAlmostEqual(d.wpm, wpm, delta=0.1 * wpm)

    def test_afc(self):
        for pitch in (470, 730):
            d = cw_rx.CwDecoder()
            self.assertEqual(d.receive(keyed(TEXT, pitch=pitch)).strip(), TEXT)
            self.assertAlmostEqual(d.tone_hz, pitch, delta=5)

    def test_noisy(self):
        # noise 0.9 at 48 kHz = about 4.3 dB SNR in 500 Hz
        for seed in (1, 2):
            self.assertTrue(decode(keyed(TEXT, noise=0.9, seed=seed)).endswith(TAIL))

    def test_noise_alone_prints_nothing(self):
        for seed in range(10, 16):
            noise = 0.3 * np.random.default_rng(seed).standard_normal(FS * 30)
            self.assertEqual(decode(noise), "", seed)

    def test_nothing_after_station_stops(self):
        out = decode(keyed(TEXT, noise=0.6, lead=5.0))
        self.assertTrue(out.endswith("PSE K"), out)

    def test_farnsworth_and_jitter(self):
        # letter vs word gaps are learned from the first few gaps
        self.assertTrue(decode(keyed(TEXT, 8, char_wpm=18)).endswith("N0CALL N0CALL PSE K"))
        self.assertEqual(decode(keyed(TEXT, 22, jitter=0.15)), TEXT)

    def test_fade(self):
        a = keyed(TEXT, noise=0.3)
        t = np.arange(len(a)) / FS
        self.assertTrue(decode(a * np.where((t > 6) & (t < 9), 0.5, 1.0)).endswith(TAIL))  # -6 dB

    def test_prosigns(self):
        self.assertEqual(decode(keyed("TU ^AR ^SK ^KN 73")), "TU <AR> <SK> <KN> 73")

    def test_small_blocks_same_as_one(self):
        a = keyed(TEXT, 25, noise=0.4)
        d = cw_rx.CwDecoder()
        self.assertEqual("".join(d.receive(a[i:i + 1000]) for i in range(0, len(a), 1000)).strip(),
                         decode(a))

    def test_interrupt_keeps_speed(self):
        d = cw_rx.CwDecoder()
        d.receive(keyed(TEXT, 30))
        d.interrupt()
        self.assertAlmostEqual(d.wpm, 30, delta=3)
        # the reply right after a transmission decodes from its first character
        self.assertEqual(d.receive(keyed("TEST DE N0CALL", 30, lead=0.5)).strip(), "TEST DE N0CALL")


if __name__ == "__main__":
    unittest.main()
