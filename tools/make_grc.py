#!/usr/bin/env python3
"""Generate icom_tx_demo.grc with icom_ptt_epy.py embedded as the Embedded Python Block.

Re-run after editing icom_ptt_epy.py:   python3 tools/make_grc.py
(Edits made to the block inside GRC are saved only in the .grc, so either edit
icom_ptt_epy.py and regenerate, or copy changes back.)

Demo flowgraph:
    Signal Source (test tone) -> Mute -> Audio Sink (radio USB codec)
    PTT toggle button -> [Icom CI-V PTT] -> mute msg -> Mute.set_mute
    CW text box -------> [Icom CI-V PTT] -> status msg -> status display
Replace the tone source with your RTTY/data modulator.
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

    blocks = [
        block("samp_rate", "variable", (200, 12), value=48000),
        block("serial_port", "variable", (320, 12), value="'/dev/serial/by-id/usb-Icom_Inc._IC-7300MK2_IC-7300MK2_<serial>-if00'",
              comment="ls -l /dev/serial/by-id/  (MK2: CI-V is the -if00 port)"),
        block("radio_addr", "variable", (480, 12), value="0xB6",
              comment="MENU > SET > Connectors > CI-V > CI-V Address"),
        block("audio_dev", "variable", (620, 12), value="'plughw:CARD=Device,DEV=0'",
              comment="aplay -L  (MK2 codec shows as C-Media USB Audio Device)"),
        block("ptt", "variable_qtgui_toggle_button_msg", (16, 120), label="PTT", type="bool",
              value="False", pressed="True", released="False", initPressed="False",
              outputmsgname="value", pressBackgroundColor="red", pressFontColor="white",
              gui_hint="0,0,1,1"),
        block("cw_wpm", "variable_qtgui_range", (16, 240), label="CW WPM", rangeType="int",
              value=20, start=6, stop=48, step=1, widget="counter_slider",
              gui_hint="1,0,1,1"),
        block("tone_freq", "variable_qtgui_range", (16, 360), label="Test tone Hz",
              rangeType="float", value=1500, start=300, stop=2700, step=10,
              widget="counter_slider", gui_hint="1,1,1,1"),
        block("usb_mod_level", "variable_qtgui_range", (16, 600), label="USB MOD Level %",
              rangeType="int", value=50, start=0, stop=100, step=1,
              widget="counter_slider", gui_hint="3,0,1,2",
              comment="Sent to the radio on start and when moved. Adjust while the "
                      "test tone transmits until ALC barely moves."),
        block("cw_text", "qtgui_edit_box_msg", (16, 480), type="string", value="''",
              label="'CW text (Enter to send, empty = abort)'", is_pair="False",
              is_static="False", key="''", gui_hint="2,0,1,2"),
        block("status_box", "qtgui_edit_box_msg", (720, 300), type="string", value="'RX'",
              label="'Status'", is_pair="False", is_static="False", key="''",
              gui_hint="0,1,1,1"),
        block("icom_ptt", "epy_block", (360, 260),
              _source_code=src, port="serial_port", baud=115200, radio_addr="radio_addr",
              lead_ms=50, tail_ms=50, tx_timeout_s=180, cw_wpm="cw_wpm",
              usb_mod_level="usb_mod_level", error_hold_s=5, lib_dir="''"),
        block("tone", "analog_sig_source_x", (360, 460), type="float", samp_rate="samp_rate",
              waveform="analog.GR_COS_WAVE", freq="tone_freq", amp=0.3, offset=0, phase=0),
        block("tx_mute", "blocks_mute_xx", (620, 480), type="float", mute="True"),
        block("audio_out", "audio_sink", (820, 480), samp_rate="samp_rate",
              device_name="audio_dev", ok_to_block="True", num_inputs=1),
    ]
    next(b for b in blocks if b["id"] == "epy_block")["states"]["_io_cache"] = repr(tuple(io))

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
            ["tone", "0", "tx_mute", "0"],
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
