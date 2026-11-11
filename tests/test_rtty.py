"""Tests for the RTTY modulator: encoding, timing, phase continuity, and a demodulator
round trip (the test decodes the generated audio back to text)."""

import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import rtty  # noqa: E402

FS = 48000


def tone_power(x, f):
    n = np.arange(len(x))
    return abs(np.dot(x, np.exp(-2j * np.pi * f / FS * n)))


def demodulate(audio, baud=45.45, mark=2125.0, space=2295.0):
    """Receiver-style decode: mark/space per 1/8 bit, then UART framing on transitions."""
    step = FS / baud / 8
    nwin = int(len(audio) // step)
    bits = []
    for i in range(nwin):
        w = audio[int(i * step):int((i + 1) * step)]
        if np.max(np.abs(w)) < 1e-3:
            bits.append(None)                         # silence
        else:
            bits.append(1 if tone_power(w, mark) > tone_power(w, space) else 0)
    codes, i = [], 0
    while i < len(bits) - 60:
        if bits[i] == 0 and (i == 0 or bits[i - 1] == 1):     # start-bit edge
            centre = i + 4
            data = [bits[centre + 8 * k] for k in range(1, 6)]
            stop = bits[centre + 8 * 6]
            if bits[centre] == 0 and stop == 1 and None not in data:
                codes.append(sum(b << k for k, b in enumerate(data)))
                i = centre + 8 * 6 + 2
                continue
        i += 1
    return codes


class TestEncoder(unittest.TestCase):
    def test_round_trip_codes(self):
        e = rtty.BaudotEncoder()
        text = "CQ CQ DE N0CALL 599 TU/73 (QSL?) 1-2.3,4:5"
        self.assertEqual(rtty.decode(e.encode(text)), text)

    def test_shifts_minimal_and_after_space(self):
        e = rtty.BaudotEncoder()
        codes = e.encode("AB 12 3")
        # LTRS A B SPACE FIGS 1 2 SPACE FIGS 3  (FIGS re-sent after a space for USOS receivers)
        self.assertEqual(codes, [rtty.LTRS, 3, 25, rtty.SPACE, rtty.FIGS, 23, 19, rtty.SPACE, rtty.FIGS, 1])

    def test_unsupported(self):
        self.assertEqual(rtty.unsupported("hi! #1"), "!#")
        self.assertEqual(rtty.decode(rtty.BaudotEncoder().encode("hi! #1")), "HI 1")


class TestGenerator(unittest.TestCase):
    def test_char_timing(self):
        g = rtty.AfskGenerator(FS)
        g.add_codes([rtty.LTRS] * 100)
        # 7.5 bits per character at 45.45 baud
        self.assertAlmostEqual(g.pending(), 100 * 7.5 / 45.45 * FS, delta=1)

    def test_phase_continuous(self):
        g = rtty.AfskGenerator(FS, amplitude=1.0)
        g.add_codes(rtty.BaudotEncoder().encode("RYRYRY"))
        x = g.read(g.pending()).astype(np.float64)
        # largest sample-to-sample step is bounded by the highest tone's slope
        self.assertLess(np.max(np.abs(np.diff(x))), 2 * np.pi * 2295 / FS * 1.01)

    def test_ramps(self):
        g = rtty.AfskGenerator(FS)
        g.start_burst(0.1)
        g.end_burst(0.1)
        x = g.read(g.pending())
        self.assertLess(abs(x[0]), 1e-3)
        self.assertLess(abs(x[-1]), 0.02)

    def test_demodulated_text(self):
        g = rtty.AfskGenerator(FS)
        e = rtty.BaudotEncoder()
        g.start_burst(0.3)
        text = "RYRY CQ TEST DE N0CALL 73"
        g.add_codes(e.encode(text))
        g.end_burst(0.2)
        audio = g.read(g.pending())
        self.assertEqual(rtty.decode(demodulate(audio)), text)


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class TestTransmitter(unittest.TestCase):
    def setUp(self):
        self.ptt = []
        self.info = []
        self.clock = FakeClock()
        self.tx = rtty.RttyTransmitter(rtty.AfskGenerator(FS), self.ptt.append, self.info.append,
                                       release_delay_s=0.5, clock=self.clock)

    def run_audio(self, seconds, block=4096):
        out = []
        for _ in range(int(seconds * FS / block)):
            out.append(self.tx.produce(block))
            self.clock.t += block / FS
        return np.concatenate(out)

    def test_waits_for_tx_then_releases(self):
        self.tx.send("TEST")
        self.assertEqual(self.ptt, [True])
        silent = self.run_audio(0.2)
        self.assertEqual(np.max(np.abs(silent)), 0.0)       # nothing before radio is in TX
        self.tx.set_radio_state("TX")
        audio = self.run_audio(3.0)
        self.assertEqual(self.ptt, [True, False])
        self.assertEqual(rtty.decode(demodulate(audio)), "\r\nTEST\r\n")

    def test_manual_ptt_not_released(self):
        self.tx.set_radio_state("TX")                      # user holds PTT
        self.tx.send("HI")
        audio = self.run_audio(2.0)
        self.assertEqual(self.ptt, [])
        self.assertIn("HI", rtty.decode(demodulate(audio)))

    def test_no_tx_drops_text(self):
        self.tx.send("TEST")
        self.run_audio(3.5)
        self.assertEqual(self.ptt, [True, False])
        self.assertIn("did not enter TX", self.info[-1])

    def test_radio_drops_tx(self):
        self.tx.send("A LONG MESSAGE THAT TAKES A WHILE")
        self.tx.set_radio_state("TX")
        self.run_audio(0.5)
        self.tx.set_radio_state("RX")                      # watchdog / error
        audio = self.run_audio(0.5)
        self.assertEqual(np.max(np.abs(audio[4096:])), 0.0)
        self.assertEqual(self.tx.state, rtty.IDLE)

    def test_empty_text_aborts(self):
        self.tx.send("A LONG MESSAGE THAT TAKES A WHILE")
        self.tx.set_radio_state("TX")
        self.run_audio(0.5)
        self.tx.send("")
        self.assertEqual(self.ptt, [True, False])
        self.assertEqual(self.tx.state, rtty.IDLE)
        self.assertEqual(np.max(np.abs(self.run_audio(0.3))), 0.0)
        self.tx.send("")                                   # idle: nothing happens
        self.assertEqual(self.ptt, [True, False])

    def test_type_ahead_same_burst(self):
        self.tx.send("ONE")
        self.tx.set_radio_state("TX")
        self.run_audio(0.3)
        self.tx.send("TWO")
        audio = self.run_audio(4.0)
        self.assertEqual(self.ptt, [True, False])
        self.assertEqual(rtty.decode(demodulate(audio)).split(), ["ONE", "TWO"])


TEXT = "RYRY CQ CQ DE N0CALL 599 TU/73 (QSL?) 1-2.3,4:5 THE QUICK BROWN FOX"


def burst(text, offset=0.0, noise=0.0, seed=1, swap=False):
    mark, space = 2125.0 + offset, 2295.0 + offset
    if swap:
        mark, space = space, mark
    g = rtty.AfskGenerator(FS, mark_hz=mark, space_hz=space)
    g.start_burst(0.5)
    g.add_codes(rtty.BaudotEncoder().encode(text))
    g.end_burst(0.5)
    a = np.concatenate([np.zeros(FS // 2), g.read(g.pending()), np.zeros(FS // 2)])
    return a + noise * np.random.default_rng(seed).standard_normal(len(a))


class TestDemodulator(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(rtty.RttyDemodulator().receive(burst(TEXT)), TEXT)

    def test_mistuned(self):
        for off in (-25, 25):
            self.assertEqual(rtty.RttyDemodulator().receive(burst(TEXT, off)), TEXT)

    def test_noisy(self):
        # noise 0.5 at 48 kHz = about +1.6 dB SNR in 3 kHz
        self.assertEqual(rtty.RttyDemodulator().receive(burst(TEXT, noise=0.5)), TEXT)

    def test_small_blocks_same_as_one(self):
        a = burst(TEXT, noise=0.2)
        d = rtty.RttyDemodulator()
        self.assertEqual("".join(d.receive(a[i:i + 997]) for i in range(0, len(a), 997)),
                         rtty.RttyDemodulator().receive(a))

    def test_squelch_hides_noise(self):
        noise = 0.3 * np.random.default_rng(5).standard_normal(FS * 20)
        self.assertEqual(rtty.RttyDemodulator().receive(noise), "")
        self.assertGreater(len(rtty.RttyDemodulator(squelch=0).receive(noise)), 0)

    def test_reverse(self):
        inverted = burst(TEXT, swap=True)
        self.assertNotEqual(rtty.RttyDemodulator().receive(inverted), TEXT)
        self.assertEqual(rtty.RttyDemodulator(reverse=True).receive(inverted), TEXT)

    def test_us_tty_figures_and_usos(self):
        d = rtty.RttyDemodulator()
        F, L = rtty.FIGS, rtty.LTRS
        self.assertEqual(d.decode([F, 9, 13, rtty.SPACE, 3]), "$! A")      # USOS: A not -
        d = rtty.RttyDemodulator(usos=False)
        self.assertEqual(d.decode([F, 9, rtty.SPACE, 3, L, 3]), "$ -A")

    def test_line_assembler(self):
        la = rtty.LineAssembler(width=10, idle_s=2.0)
        self.assertEqual(la.feed("\r\nCQ CQ\r\nTHE QUICK BROWN", 0.0), ["CQ CQ", "THE QUICK"])
        self.assertEqual(la.poll(1.0), [])
        self.assertEqual(la.poll(2.5), ["BROWN"])
        self.assertEqual(la.feed("ABCDEFGHIJKLM", 3.0), ["ABCDEFGHIJ"])


if __name__ == "__main__":
    unittest.main()
