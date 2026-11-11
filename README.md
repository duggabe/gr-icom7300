# gr-icom7300

T/R (PTT) switching and CW keying for an Icom IC-7300 / IC-7300MK2 over CI-V on USB,
from Python and GNU Radio 3.10. It writes CI-V directly with pyserial and uses Hamlib
only as a reference.

Intended for digital data, RTTY (AFSK over USB audio) and Morse code. Not designed for voice.

## Files

| File | Purpose |
|---|---|
| `icom_civ.py` | CI-V protocol: frames, serial reader thread, PTT, CW keyer, mode, break-in. Also a CLI test tool. No GNU Radio dependency. |
| `civ_controller.py` | T/R sequencing: lead/tail timing, TX watchdog, CW state, audio mute. No GNU Radio dependency. |
| `icom_ptt_epy.py` | Source of the GNU Radio Embedded Python Block (message ports only). |
| `icom_tx_demo.grc` | Demo flowgraph: PTT button, CW text box, WPM and USB MOD Level sliders, status, test tone → Mute → radio USB audio. |
| `tools/make_grc.py` | Regenerates `icom_tx_demo.grc` after editing `icom_ptt_epy.py`. |
| `tests/` | Tests against a simulated radio, including a pty-based GNU Radio end-to-end test. |

## Radio setup (MENU › SET › Connectors)

- **CI-V Baud Rate:** 115200 (not Auto). **CI-V Address:** MK2 default `B6` (code default); original IC-7300 is `94`.
- **CI-V USB Echo Back:** either setting works.
- **USB SEND / USB Keying (CW/RTTY):** OFF, so only CI-V keys the radio.
- **DATA MOD:** USB. Use USB-D (or LSB-D for RTTY) for audio-based modes.
- Set the USB MOD level so ALC barely moves; control power with RF power.
  RTTY and data are 100% duty cycle, so run reduced power.
- **CW via the radio's keyer:** CW mode with break-in SEMI or FULL. The `cw` CLI command sets both.

## Host setup

```
sudo usermod -aG dialout $USER     # then log out/in
ls -l /dev/serial/by-id/           # MK2: two ports; CI-V is ...IC-7300MK2_<serial>-if00 (ttyACM)
aplay -L | grep CARD=Device        # MK2 codec: C-Media "USB Audio Device" -> plughw:CARD=Device,DEV=0
amixer -c 2 set Speaker 100%       # codec playback at 0 dB; set drive with USB MOD Level instead
```

The codec's ALSA **Speaker** control (radio TX audio) defaulted to 38% (-23 dB), too low
to reach ALC even at 100% USB MOD Level. The card number may change between boots or
USB ports (`aplay -l`). Only one program can open the codec at a time, so close any
earlier flowgraph run before starting another.

## Command-line tests (use a dummy load)

```
python3 icom_civ.py --port /dev/serial/by-id/<radio> --debug id      # read CI-V address
python3 icom_civ.py --port ... status
python3 icom_civ.py --port ... tx 1                                    # key 1 s (max 10)
python3 icom_civ.py --port ... rx
python3 icom_civ.py --port ... cw "TEST DE N0CALL" --wpm 20
python3 icom_civ.py --port ... mode USB --data
python3 icom_civ.py --port ... usbmod          # read USB MOD Level; 'usbmod 45' sets it
```

## GNU Radio

Open `icom_tx_demo.grc` from this directory, so the generated block can import
`icom_civ.py`. Otherwise set the block's `lib_dir`. Then set the `serial_port`,
`radio_addr` and `audio_dev` variables and replace the test tone with your modulator.

Block **Icom CI-V PTT**:

- in `ptt`: bool, or the `(name . value)` pair from QT GUI Toggle Button
- in `cw`: text for the radio's keyer; empty text aborts
- out `status`: `RX` / `LEAD` / `TX` / `TAIL` / `CW` / `ERROR: ...`
- out `mute`: connect to a Mute block's `set_mute` in the TX audio path

Sequence: key → confirm TX → `lead_ms` → un-mute → … release → mute → `tail_ms` → unkey.
The watchdog unkeys after `tx_timeout_s`. `stop()` always unkeys.

`cw_wpm` and `usb_mod_level` can be driven by GRC variables (e.g. QT GUI Range). Each is
sent to the radio on start and whenever it changes; `0` / `-1` leaves the radio's
setting alone. The demo's USB MOD Level slider starts at 50%, so starting the flowgraph
sets the radio to 50%. Adjust it while the test tone transmits until ALC barely moves.

## Tests

```
python3 -m unittest discover -s tests -v
```

## Hardware status

Verified on an IC-7300MK2 (2026-11-10): CI-V address `B6` on port `-if00`; `-if02` does
not answer CI-V; echo back off; audio codec is C-Media `CARD=Device`; ID and PTT status
reads work; CI-V PTT keys and unkeys with confirmed TX/RX; the CW keyer (CW mode, semi
break-in, speed) sends cleanly, and text over 30 characters sent as back-to-back chunks
has no gap. USB MOD Level is SET-menu item `1A 05 0081` on the MK2 (level 0–255,
radio shows floor(level×100/255) %); read and write verified. The demo flowgraph's TX
audio reaches the radio and the USB MOD Level slider changes ALC while transmitting.
