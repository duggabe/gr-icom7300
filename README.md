# gr-icom7300

T/R (PTT) switching and CW keying for an Icom IC-7300 / IC-7300MK2 over CI-V on USB,
from Python and GNU Radio 3.10. It writes CI-V directly with pyserial and uses Hamlib
only as a reference.

Intended for digital data, RTTY (AFSK over USB audio, built-in modulator) and Morse code.
Not designed for voice.

## Files

| File | Purpose |
|---|---|
| `icom_civ.py` | CI-V protocol: frames, serial reader thread, PTT, CW keyer, mode, break-in. Also a CLI test tool. No GNU Radio dependency. |
| `civ_controller.py` | T/R sequencing: lead/tail timing, TX watchdog, CW state, audio mute. No GNU Radio dependency. |
| `icom_ptt_epy.py` | Source of the GNU Radio Embedded Python Block (message ports only). |
| `rtty.py` | RTTY modulator: Baudot encoder, phase-continuous AFSK, PTT coordination. No GNU Radio dependency. |
| `rtty_mod_epy.py` | Source of the RTTY AFSK Mod Embedded Python Block (float audio out). |
| `console_router_epy.py` | Source of the Console Router Embedded Python Block: connects the gr-webserver browser console to CW and RTTY and shows radio status there. |
| `icom_tx_demo.grc` | Demo flowgraph: PTT button, WPM and USB MOD Level sliders, status, gr-webserver console for CW and RTTY text, test tone + RTTY → Mute → radio USB audio. |
| `tools/make_grc.py` | Builds `icom_tx_demo.grc` from scratch (overwrites it), or with `--update-code [FILE.grc]` copies the current block code into an existing flowgraph and keeps your GRC edits. See "Changing the flowgraph". |
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
sets the radio to 50%. Tick **Test tone**, press PTT and adjust it until ALC barely moves.

## RTTY

Block **RTTY AFSK Mod**: 45.45 baud, 170 Hz shift, 1.5 stop bits, Baudot (characters common
to ITA2 and US-TTY: letters, digits, space and `- ? : ( ) . , /`; others are skipped).

- Put the radio in **LSB-D** (`icom_civ.py ... mode LSB --data`): audio mark 2125 / space
  2295 Hz then gives mark on the higher RF frequency, the normal sense. For USB-D, swap them.
- Type in the gr-webserver console (mode RTTY) and press Enter. The modulator requests PTT, waits for the PTT
  block to report TX, sends mark idle + LTRS, the text on its own line, a mark tail, then
  releases PTT after `release_delay_s` (covers about 0.4 s of audio buffering).
- Text entered while sending is appended to the same transmission. An empty line aborts.
- If you hold PTT yourself, the modulator sends without keying or releasing.
- The RTTY amplitude (0.3) matches the test tone, so the USB MOD Level set with the tone
  carries over.

## Text console (gr-webserver)

Text in and out goes through [gr-webserver](https://github.com/duggabe/gr-webserver), a
terminal-style browser page, instead of QT GUI Message Edit Boxes. The demo uses gr-webserver
v2.1 (ZMQ PUB/SUB): a ZMQ SUB Message Source on `tcp://127.0.0.1:50251` (bind off) feeds the
**Console Router**, and a ZMQ PUB Message Sink on `tcp://127.0.0.1:50252` (bind on) carries its
display lines back. For gr-webserver v1.0.16, replace them with ZMQ PULL Message Source and
ZMQ PUSH Message Sink blocks (same addresses and bind settings).

1. Start gr-webserver in its own terminal: `cd ~/gr-webserver && node index.js`. A browser
   tab opens.
2. Start the flowgraph. The console shows `Mode RTTY. Type /help for commands.`
3. Type a line and press Enter. It goes out in the current mode.

| Console input | Action |
|---|---|
| text | send as RTTY or CW (current mode) |
| `/rtty`, `/cw` | switch mode (starts in the block's `mode` parameter, default RTTY) |
| `/stop` or an empty line | abort the CW or RTTY transmission in progress |
| `/help` | list the commands |

The console shows `TX`, `TX (CW keyer)`, `RX`, errors from the PTT block, and RTTY notices
such as skipped characters. gr-webserver keeps the last 20 lines. Its one-byte length field
limits a line to 255 ASCII characters.

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
Sidetone: SET > CW-KEY Set, items `0218` (level) and `0219` (limit to AF gain).

RTTY: the modulator keys the radio, sends, and unkeys automatically (verified on the radio).
Not yet verified: decoding the transmitted RTTY with a separate receiver.

## Changing the flowgraph

Once you edit the flowgraph in GRC, the saved `.grc` is the master copy. Don't run
`tools/make_grc.py` without options after that: it rebuilds the demo from scratch.

| You changed | Do this |
|---|---|
| The flowgraph in GRC | Save in GRC. Nothing else. |
| `icom_civ.py`, `civ_controller.py`, `rtty.py` | Nothing. They are imported at run time. |
| `icom_ptt_epy.py` or `rtty_mod_epy.py` | Close the flowgraph in GRC, run `python3 tools/make_grc.py --update-code` (or `--update-code my_flowgraph.grc`), reopen in GRC. |

`--update-code` finds the blocks by their code ("Icom CI-V PTT", "RTTY AFSK Mod"), so renamed
blocks and copies in other flowgraphs work too. Parameter values set in GRC are kept; the
previous file is saved as `FILE.grc.bak`. Commit the `.grc` before regenerating, so
`git checkout icom_tx_demo.grc` can undo anything.
