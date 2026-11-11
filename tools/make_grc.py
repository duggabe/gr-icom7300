#!/usr/bin/env python3
"""Generate icom_tx_demo.grc with icom_ptt_epy.py and rtty_mod_epy.py embedded as
Embedded Python Blocks.

Re-run after editing either block file:   python3 tools/make_grc.py
(Edits made to a block inside GRC are saved only in the .grc, so either edit the
.py file and regenerate, or copy changes back.) Regenerating overwrites the .grc.

Demo flowgraph:
    test tone (checkbox) --+
                           +--> Add -> Mute -> Audio Sink (radio USB codec)
    [RTTY AFSK Mod] -------+
    PTT button ------------> [Icom CI-V PTT] -> mute msg -> Mute.set_mute
    CW text box -----------> [Icom CI-V PTT] -> status msg -> status box, RTTY Mod
    RTTY text box ---------> [RTTY AFSK Mod] -> ptt msg -> [Icom CI-V PTT]
"""

import os
import sys

import yaml

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)


def block(name, id_, coord, **params):
    return {"name": name, "id": id_, "parameters": {k: str(v) for k, v in params.items()},
            "states": {"coordinate": list(coord), "rotation": 0, "state": "enabled"}}


def main():
    from gnuradio.grc.core.utils import epy_block_io

    src = open(os.path.join(ROOT, "icom_ptt_epy.py")).read()
    io = epy_block_io.extract(src)
    rtty_src = open(os.path.join(ROOT, "rtty_mod_epy.py")).read()
    rtty_io = epy_block_io.extract(rtty_src)
    small = dict(maxoutbuf=1024)   # keep audio buffering (and so PTT release delay) short

    blocks = [
        block("samp_rate", "variable", (200, 12), value=48000),
        block("serial_port", "variable", (320, 12), value="'/dev/serial/by-id/usb-Icom_Inc._IC-7300MK2_IC-7300MK2_<serial>-if00'",
              comment="ls -l /dev/serial/by-id/  (MK2: CI-V is the -if00 port)"),
        block("radio_addr", "variable", (480, 12), value="0xB6",
              comment="MENU > SET > Connectors > CI-V > CI-V Address"),
        block("audio_dev", "variable", (620, 12), value="'plughw:CARD=Device,DEV=0'",
              comment="aplay -L  (MK2 codec shows as C-Media USB Audio Device)"),
        # GUI rows: 0 PTT | status   1 USB MOD | tone Hz   2 tone on | CW WPM   3 CW text   4 RTTY text
        block("ptt", "variable_qtgui_toggle_button_msg", (16, 120), label="PTT", type="bool",
              value="False", pressed="True", released="False", initPressed="False",
              outputmsgname="value", pressBackgroundColor="red", pressFontColor="white",
              gui_hint="0,0,1,1"),
        block("status_box", "qtgui_edit_box_msg", (900, 300), type="string", value="'RX'",
              label="'Status'", is_pair="False", is_static="False", key="''",
              gui_hint="0,1,1,1"),
        block("usb_mod_level", "variable_qtgui_range", (16, 600), label="USB MOD Level %",
              rangeType="int", value=50, start=0, stop=100, step=1,
              widget="counter_slider", gui_hint="1,0,1,1",
              comment="Sent to the radio on start and when moved. Adjust while the "
                      "test tone transmits until ALC barely moves."),
        block("tone_freq", "variable_qtgui_range", (16, 360), label="Test tone Hz",
              rangeType="float", value=1500, start=300, stop=2700, step=10,
              widget="counter_slider", gui_hint="1,1,1,1"),
        block("tone_on", "variable_qtgui_check_box", (16, 720), label="Test tone", type="bool",
              value="False", **{"true": "True", "false": "False"}, gui_hint="2,0,1,1"),
        block("cw_wpm", "variable_qtgui_range", (16, 240), label="CW WPM", rangeType="int",
              value=20, start=6, stop=48, step=1, widget="counter_slider",
              gui_hint="2,1,1,1"),
        block("cw_text", "qtgui_edit_box_msg", (16, 480), type="string", value="''",
              label="'CW text (Enter to send, empty = abort)'", is_pair="False",
              is_static="False", key="''", gui_hint="3,0,1,2"),
        block("rtty_text", "qtgui_edit_box_msg", (16, 840), type="string", value="''",
              label="'RTTY text (Enter to send; radio in LSB-D)'", is_pair="False",
              is_static="False", key="''", gui_hint="4,0,1,2"),
        block("icom_ptt", "epy_block", (420, 260),
              _source_code=src, port="serial_port", baud=115200, radio_addr="radio_addr",
              lead_ms=50, tail_ms=50, tx_timeout_s=180, cw_wpm="cw_wpm",
              usb_mod_level="usb_mod_level", error_hold_s=5, lib_dir="''"),
        block("rtty_mod", "epy_block", (420, 700),
              _source_code=rtty_src, samp_rate="samp_rate", baud=45.45, mark_hz=2125.0,
              space_hz=2295.0, amplitude=0.3, preamble_s=0.3, postamble_s=0.2,
              release_delay_s=0.5, lib_dir="''", **small),
        block("tone", "analog_sig_source_x", (420, 520), type="float", samp_rate="samp_rate",
              waveform="analog.GR_COS_WAVE", freq="tone_freq", amp="0.3 if tone_on else 0",
              offset=0, phase=0, **small),
        block("audio_sum", "blocks_add_xx", (700, 600), type="float", num_inputs=2, vlen=1,
              **small),
        block("tx_mute", "blocks_mute_xx", (860, 600), type="float", mute="True", **small),
        block("audio_out", "audio_sink", (1040, 600), samp_rate="samp_rate",
              device_name="audio_dev", ok_to_block="True", num_inputs=1),
    ]
    next(b for b in blocks if b["name"] == "icom_ptt")["states"]["_io_cache"] = repr(tuple(io))
    next(b for b in blocks if b["name"] == "rtty_mod")["states"]["_io_cache"] = repr(tuple(rtty_io))

    fg = {
        "options": {
            "parameters": {"id": "icom_tx_demo", "title": "Icom IC-7300 TX demo",
                           "generate_options": "qt_gui", "output_language": "python",
                           "run": "True", "run_options": "prompt", "catch_exceptions": "True",
                           "window_size": "(600,300)", "placement": "(0,0)"},
            "states": {"coordinate": [16, 12], "rotation": 0, "state": "enabled"},
        },
        "blocks": blocks,
        "connections": [
            ["ptt", "state", "icom_ptt", "ptt"],
            ["cw_text", "msg", "icom_ptt", "cw"],
            ["icom_ptt", "status", "status_box", "val"],
            ["icom_ptt", "mute", "tx_mute", "set_mute"],
            ["rtty_text", "msg", "rtty_mod", "text"],
            ["icom_ptt", "status", "rtty_mod", "status"],
            ["rtty_mod", "ptt", "icom_ptt", "ptt"],
            ["tone", "0", "audio_sum", "0"],
            ["rtty_mod", "0", "audio_sum", "1"],
            ["audio_sum", "0", "tx_mute", "0"],
            ["tx_mute", "0", "audio_out", "0"],
        ],
        "metadata": {"file_format": 1, "grc_version": "3.10.12.0"},
    }
    out = os.path.join(ROOT, "icom_tx_demo.grc")
    with open(out, "w") as f:
        yaml.safe_dump(fg, f, sort_keys=False, width=100)
    print("wrote", out)


if __name__ == "__main__":
    main()
