"""Minimal Icom CI-V interface for T/R (PTT) control over USB serial.

Written for the IC-7300 / IC-7300MK2, using Hamlib's rigs/icom as a reference.
On the MK2, CI-V is the first USB serial port (by-id name ending -if00), address B6.
Has no GNU Radio dependency, so it can be tested on its own from the command line:

    python3 icom_civ.py --port /dev/ttyUSB0 id
    python3 icom_civ.py --port /dev/ttyUSB0 status
    python3 icom_civ.py --port /dev/ttyUSB0 tx 2      # key for 2 s, then unkey
    python3 icom_civ.py --port /dev/ttyUSB0 rx        # force unkey
    python3 icom_civ.py --port /dev/ttyUSB0 cw "CQ TEST DE N0CALL" --wpm 20
    python3 icom_civ.py --port /dev/ttyUSB0 mode USB --data
    python3 icom_civ.py --port /dev/ttyUSB0 usbmod        # read; 'usbmod 45' to set

CI-V frame:  FE FE <to> <from> <cmd> [<sub>] [<data>...] FD
"""

from __future__ import annotations

import argparse
import queue
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

PREAMBLE = 0xFE
EOM = 0xFD
ACK = 0xFB
NAK = 0xFA
BROADCAST = 0x00

DEFAULT_RADIO_ADDR = 0xB6   # IC-7300MK2 default (verified); original IC-7300 is 0x94
DEFAULT_CTRL_ADDR = 0xE0    # conventional controller address

CMD_PTT = 0x1C
SUB_PTT = 0x00
CMD_READ_ID = 0x19
SUB_READ_ID = 0x00
CMD_SEND_CW = 0x17          # data: up to 30 chars; 0xFF = stop sending
CW_STOP = 0xFF
CW_MAX_CHUNK = 30
CMD_SET_MODE = 0x06         # data: mode, filter (1-3)
CMD_LEVEL = 0x14
SUB_KEY_SPEED = 0x0C        # 0000-0255 BCD -> 6-48 WPM
CMD_FUNC = 0x16
SUB_BREAK_IN = 0x47         # 00 off, 01 semi, 02 full
CMD_EXT = 0x1A
SUB_DATA_MODE = 0x06        # data: 00/01 (off/on), filter (00 when off, else 1-3)
SUB_MENU = 0x05             # 1A 05 <item, 4-digit BCD> [data]: SET menu items
MENU_USB_MOD_LEVEL = 81     # IC-7300MK2 (verified on hardware); level 0000-0255 = 0-100%

MODES = {"LSB": 0x00, "USB": 0x01, "AM": 0x02, "CW": 0x03, "RTTY": 0x04,
         "FM": 0x05, "CW-R": 0x07, "RTTY-R": 0x08}
DATA_CAPABLE = {"LSB", "USB", "AM", "FM"}
BREAK_IN = {"OFF": 0x00, "SEMI": 0x01, "FULL": 0x02}
CW_WPM_MIN, CW_WPM_MAX = 6, 48

# Characters the radio's CW keyer accepts over CI-V (command 17). '^' joins the next two
# characters into one prosign without a letter gap, e.g. "^AR".
MORSE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.", "G": "--.",
    "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..", "M": "--", "N": "-.",
    "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.", "S": "...", "T": "-", "U": "..-",
    "V": "...-", "W": ".--", "X": "-..-", "Y": "-.--", "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-", "5": ".....",
    "6": "-....", "7": "--...", "8": "---..", "9": "----.",
    "/": "-..-.", "?": "..--..", ".": ".-.-.-", "-": "-....-", ",": "--..--",
    ":": "---...", "'": ".----.", "(": "-.--.", ")": "-.--.-", "=": "-...-",
    "+": ".-.-.", '"': ".-..-.", "@": ".--.-.",
}


def normalize_cw(text: str) -> str:
    """Upper-case and validate CW text; raises ValueError on unsupported characters."""
    t = " ".join(text.upper().split())
    bad = sorted({c for c in t if c not in MORSE and c not in " ^"})
    if bad:
        raise ValueError(f"unsupported CW character(s): {''.join(bad)!r}")
    return t


def cw_duration(text: str, wpm: float) -> float:
    """Estimated keying time in seconds (PARIS timing: 1 unit = 1.2/wpm s)."""
    units = 0
    pending_gap = 0     # gap owed before the next character (3 letter / 7 word)
    arm = False         # '^' seen: the next two characters are joined
    joined = False      # gap after the current character is only 1 unit
    for c in normalize_cw(text):
        if c == " ":
            if pending_gap:
                pending_gap = 7
            continue
        if c == "^":
            arm = True
            continue
        if pending_gap:
            units += 1 if joined and pending_gap == 3 else pending_gap
        joined, arm = arm, False
        pending_gap = 3
        code = MORSE[c]
        units += sum(1 if e == "." else 3 for e in code) + (len(code) - 1)
    return units * 1.2 / wpm


def split_cw(text: str, size: int = CW_MAX_CHUNK) -> list[str]:
    """Split text into chunks of at most `size` characters, preferring word boundaries."""
    chunks: list[str] = []
    t = normalize_cw(text)
    while t:
        if len(t) <= size:
            chunks.append(t)
            break
        cut = t.rfind(" ", 0, size)     # cut < size, so chunk incl. trailing space <= size
        if cut <= 0:
            chunks.append(t[:size])
            t = t[size:]
            continue
        # keep the trailing space so words in consecutive chunks stay separated
        chunks.append(t[:cut + 1])
        t = t[cut + 1:].lstrip(" ")
    return chunks


def wpm_to_civ(wpm: int) -> bytes:
    """Key speed in WPM -> 2-byte BCD level 0000-0255 (Hamlib's mapping)."""
    wpm = min(max(int(wpm), CW_WPM_MIN), CW_WPM_MAX)
    level = round((wpm - CW_WPM_MIN) * 255 / (CW_WPM_MAX - CW_WPM_MIN))
    return bcd4(level)


def bcd4(n: int) -> bytes:
    """0-9999 -> 2 bytes BCD, big-endian (e.g. 81 -> 00 81, 128 -> 01 28)."""
    if not 0 <= n <= 9999:
        raise ValueError(f"BCD value out of range: {n}")
    s = f"{n:04d}"
    return bytes([int(s[0]) << 4 | int(s[1]), int(s[2]) << 4 | int(s[3])])


def from_bcd(data: bytes) -> int:
    """Big-endian BCD bytes -> int."""
    n = 0
    for b in data:
        hi, lo = b >> 4, b & 0x0F
        if hi > 9 or lo > 9:
            raise ValueError(f"not BCD: {data.hex(' ')}")
        n = n * 100 + hi * 10 + lo
    return n


# Icom 0-255 levels <-> percent as the radio displays them. Matches readings from the
# MK2: 50% = 128, 51% = 132, 60% = 153 (display = floor(level * 100 / 255)).
def percent_to_level(pct: float) -> int:
    pct = min(max(pct, 0), 100)
    return min(255, -(-int(round(pct * 255)) // 100))   # ceil(pct * 2.55)


def level_to_percent(level: int) -> int:
    return level * 100 // 255


class CivError(Exception):
    """CI-V protocol or communication error."""


class CivTimeout(CivError):
    """No reply from the radio within the timeout."""


class CivNak(CivError):
    """Radio rejected the command (FA)."""


@dataclass(frozen=True)
class Frame:
    to: int
    frm: int
    payload: bytes      # cmd, sub-cmd and data bytes (everything between addresses and FD)

    @property
    def cmd(self) -> int:
        return self.payload[0] if self.payload else -1

    def encode(self) -> bytes:
        return build_frame(self.to, self.frm, self.payload)

    def __str__(self) -> str:
        return " ".join(f"{b:02X}" for b in self.encode())


def build_frame(to: int, frm: int, payload: bytes) -> bytes:
    return bytes([PREAMBLE, PREAMBLE, to, frm]) + bytes(payload) + bytes([EOM])


class FrameParser:
    """Incremental parser: feed raw bytes, get complete frames back."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[Frame]:
        self._buf.extend(data)
        frames: list[Frame] = []
        while True:
            start = self._buf.find(bytes([PREAMBLE, PREAMBLE]))
            if start < 0:
                # keep a trailing FE in case the next chunk starts with the second one
                del self._buf[: max(0, len(self._buf) - 1)]
                return frames
            del self._buf[:start]
            end = self._buf.find(bytes([EOM]))
            if end < 0:
                return frames
            body = bytes(self._buf[:end]).lstrip(bytes([PREAMBLE]))  # tolerate extra FEs
            del self._buf[: end + 1]
            # body = to, from, payload... ; "FE FE FC FD" (collision jam) etc. is dropped here
            if len(body) >= 3:
                frames.append(Frame(body[0], body[1], body[2:]))


class CivPort:
    """CI-V connection to one radio.

    A background thread reads the port. Replies addressed to us go to the
    command/response path. Broadcast frames (CI-V Transceive) go to `on_unsolicited`.
    Echoes of our own frames (if CI-V USB Echo Back is ON) are ignored.

    `serial_port` may be any object with read(n)/write(b)/close() (used by the tests);
    if None, pyserial opens `port`.
    """

    def __init__(
        self,
        port: str = "",
        baud: int = 115200,
        radio_addr: int = DEFAULT_RADIO_ADDR,
        ctrl_addr: int = DEFAULT_CTRL_ADDR,
        timeout: float = 0.5,
        retries: int = 2,
        on_unsolicited: Optional[Callable[[Frame], None]] = None,
        serial_port=None,
        debug: bool = False,
    ) -> None:
        self.port = port
        self.baud = baud
        self.radio_addr = radio_addr
        self.ctrl_addr = ctrl_addr
        self.timeout = timeout
        self.retries = retries
        self.on_unsolicited = on_unsolicited
        self.debug = debug
        self._ser = serial_port
        self._replies: "queue.Queue[Frame]" = queue.Queue()
        self._lock = threading.Lock()          # one transaction at a time
        self._stop = threading.Event()
        self._reader: Optional[threading.Thread] = None

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> "CivPort":
        if self._reader is not None:
            return self                    # already open
        if self._ser is None:
            import serial  # pyserial
            # rtscts/dsrdtr off and RTS/DTR low: if the radio's USB SEND/USB Keying
            # is assigned to RTS or DTR, asserting them would key the transmitter.
            ser = serial.Serial()
            ser.port = self.port
            ser.baudrate = self.baud
            ser.timeout = 0.05
            ser.rtscts = False
            ser.dsrdtr = False
            ser.rts = False
            ser.dtr = False
            # exclusive: a second program (e.g. the CLI while a flowgraph runs) would
            # steal replies from this one; make it fail clearly at open instead
            ser.exclusive = True
            ser.open()
            self._ser = ser
        self._stop.clear()
        self._reader = threading.Thread(target=self._read_loop, name="civ-reader", daemon=True)
        self._reader.start()
        return self

    def close(self) -> None:
        self._stop.set()
        if self._reader is not None:
            self._reader.join(timeout=1.0)
            self._reader = None
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    def __enter__(self) -> "CivPort":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- reader ------------------------------------------------------------
    def _read_loop(self) -> None:
        parser = FrameParser()
        while not self._stop.is_set():
            try:
                data = self._ser.read(64)
            except Exception as e:  # port unplugged etc.
                self._log(f"read error: {e}")
                time.sleep(0.2)
                continue
            if not data:
                continue
            for f in parser.feed(data):
                self._log(f"<- {f}")
                if f.frm == self.ctrl_addr:
                    continue                      # echo of our own frame
                if f.frm != self.radio_addr:
                    continue                      # another radio on the bus
                if f.to == self.ctrl_addr:
                    self._replies.put(f)
                elif f.to == BROADCAST and self.on_unsolicited:
                    try:
                        self.on_unsolicited(f)
                    except Exception as e:
                        self._log(f"on_unsolicited error: {e}")

    def _log(self, msg: str) -> None:
        if self.debug:
            print(f"[civ] {msg}", file=sys.stderr)

    # -- transactions ------------------------------------------------------
    def transact(self, payload: bytes, expect: Optional[bytes] = None) -> Frame:
        """Send a command and wait for its reply.

        expect=None: a set command, so the reply must be ACK (FB); NAK raises CivNak.
        expect=b'..': a read command, so the reply must start with these bytes (cmd+sub).
        """
        if self._ser is None:
            raise CivError("port not open")
        frame = build_frame(self.radio_addr, self.ctrl_addr, payload)
        with self._lock:
            last_err: CivError = CivTimeout("no reply")
            for _attempt in range(self.retries + 1):
                self._drain()
                self._log(f"-> {' '.join(f'{b:02X}' for b in frame)}")
                self._ser.write(frame)
                try:
                    return self._wait_reply(expect)
                except CivTimeout as e:
                    last_err = e
            raise last_err

    def _wait_reply(self, expect: Optional[bytes]) -> Frame:
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CivTimeout(f"no reply from radio 0x{self.radio_addr:02X} within {self.timeout}s")
            try:
                f = self._replies.get(timeout=remaining)
            except queue.Empty:
                continue
            if f.cmd == NAK:
                raise CivNak("radio returned NAK (FA)")
            if expect is None:
                if f.cmd == ACK:
                    return f
            elif f.payload.startswith(expect):
                return f
            # anything else is a stale/unrelated reply: keep waiting

    def _drain(self) -> None:
        while True:
            try:
                self._replies.get_nowait()
            except queue.Empty:
                return

    # -- radio functions ---------------------------------------------------
    def set_ptt(self, tx: bool) -> None:
        self.transact(bytes([CMD_PTT, SUB_PTT, 0x01 if tx else 0x00]))

    def get_ptt(self) -> bool:
        f = self.transact(bytes([CMD_PTT, SUB_PTT]), expect=bytes([CMD_PTT, SUB_PTT]))
        if len(f.payload) < 3:
            raise CivError(f"short PTT reply: {f}")
        return f.payload[2] == 0x01

    def read_id(self) -> int:
        """Return the radio's CI-V address (command 19 00)."""
        f = self.transact(bytes([CMD_READ_ID, SUB_READ_ID]), expect=bytes([CMD_READ_ID, SUB_READ_ID]))
        if len(f.payload) < 3:
            raise CivError(f"short ID reply: {f}")
        return f.payload[2]

    # CW keyer: the radio must be in CW (or CW-R) mode with break-in SEMI or FULL,
    # otherwise it sounds sidetone but does not transmit.
    def send_cw(self, text: str) -> None:
        """Send text with the radio's internal keyer (30-char chunks, as Hamlib does)."""
        for chunk in split_cw(text):
            self.transact(bytes([CMD_SEND_CW]) + chunk.encode("ascii"))

    def stop_cw(self) -> None:
        self.transact(bytes([CMD_SEND_CW, CW_STOP]))

    def set_cw_speed(self, wpm: int) -> None:
        self.transact(bytes([CMD_LEVEL, SUB_KEY_SPEED]) + wpm_to_civ(wpm))

    def set_break_in(self, mode: str) -> None:
        self.transact(bytes([CMD_FUNC, SUB_BREAK_IN, BREAK_IN[mode.upper()]]))

    # SET menu items (1A 05). Item numbers are model-specific.
    def read_menu(self, item: int) -> bytes:
        key = bytes([CMD_EXT, SUB_MENU]) + bcd4(item)
        return self.transact(key, expect=key).payload[len(key):]

    def write_menu(self, item: int, data: bytes) -> None:
        self.transact(bytes([CMD_EXT, SUB_MENU]) + bcd4(item) + data)

    def get_usb_mod_level(self) -> int:
        """USB MOD Level in percent, as shown in MENU > SET > Connectors."""
        return level_to_percent(from_bcd(self.read_menu(MENU_USB_MOD_LEVEL)))

    def set_usb_mod_level(self, pct: float) -> int:
        """Set USB MOD Level (0-100 %); returns the value the radio now reports."""
        self.write_menu(MENU_USB_MOD_LEVEL, bcd4(percent_to_level(pct)))
        return self.get_usb_mod_level()

    def set_mode(self, mode: str, data: bool = False, filt: int = 1) -> None:
        """Set mode and filter (1-3), and the DATA (-D) flag, e.g. set_mode('USB', data=True)."""
        mode = mode.upper()
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}; choose from {', '.join(MODES)}")
        if data and mode not in DATA_CAPABLE:
            raise ValueError(f"DATA is not available in {mode}")
        if filt not in (1, 2, 3):
            raise ValueError("filter must be 1, 2 or 3")
        self.transact(bytes([CMD_SET_MODE, MODES[mode], filt]))
        if mode in DATA_CAPABLE:
            self.transact(bytes([CMD_EXT, SUB_DATA_MODE]) + (bytes([0x01, filt]) if data else b"\x00\x00"))


# -- command-line test tool ------------------------------------------------
MAX_TX_SECONDS = 10.0


def _hex(s: str) -> int:
    return int(s, 16)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Icom CI-V PTT test tool")
    ap.add_argument("--port", required=True, help="e.g. /dev/serial/by-id/usb-Icom_Inc._IC-7300MK2_...-if00")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--addr", type=_hex, default=DEFAULT_RADIO_ADDR, help="radio CI-V address, hex (default B6 = IC-7300MK2; IC-7300 is 94)")
    ap.add_argument("--ctrl", type=_hex, default=DEFAULT_CTRL_ADDR, help="controller address, hex (default E0)")
    ap.add_argument("--debug", action="store_true", help="print raw frames")
    sub = ap.add_subparsers(dest="action", required=True)
    sub.add_parser("id", help="read the radio's CI-V address")
    sub.add_parser("status", help="read TX/RX state")
    sub.add_parser("rx", help="unkey (force receive)")
    p_tx = sub.add_parser("tx", help=f"key for N seconds (max {MAX_TX_SECONDS:g}), then unkey")
    p_tx.add_argument("seconds", type=float)
    p_cw = sub.add_parser("cw", help="send text with the radio's keyer (sets CW mode + semi break-in)")
    p_cw.add_argument("text")
    p_cw.add_argument("--wpm", type=int, default=20)
    p_cw.add_argument("--no-setup", action="store_true", help="don't change mode/break-in")
    p_um = sub.add_parser("usbmod", help="read USB MOD Level, or set it: 'usbmod 45'")
    p_um.add_argument("percent", type=float, nargs="?")
    p_mode = sub.add_parser("mode", help="set mode, e.g. 'mode USB --data' or 'mode CW'")
    p_mode.add_argument("mode", choices=list(MODES))
    p_mode.add_argument("--data", action="store_true")
    p_mode.add_argument("--filter", type=int, default=1, choices=(1, 2, 3))
    args = ap.parse_args(argv)

    civ = CivPort(args.port, args.baud, args.addr, args.ctrl, debug=args.debug)
    try:
        civ.open()
    except Exception as e:
        print(f"error: cannot open {args.port}: {e}", file=sys.stderr)
        if "lock" in str(e).lower():
            print("  (another program, e.g. a running flowgraph, has the port open)", file=sys.stderr)
        return 1
    with civ:
        try:
            if args.action == "id":
                print(f"radio CI-V address: {civ.read_id():02X}")
            elif args.action == "status":
                print("TX" if civ.get_ptt() else "RX")
            elif args.action == "rx":
                civ.set_ptt(False)
                print("RX" if not civ.get_ptt() else "WARNING: still TX")
            elif args.action == "tx":
                secs = min(max(args.seconds, 0.0), MAX_TX_SECONDS)
                try:
                    civ.set_ptt(True)
                    print(f"TX (confirmed={civ.get_ptt()}) for {secs:g}s ...")
                    time.sleep(secs)
                finally:
                    civ.set_ptt(False)       # always unkey, even on Ctrl-C
                print("RX" if not civ.get_ptt() else "WARNING: still TX")
            elif args.action == "cw":
                text = normalize_cw(args.text)
                if not args.no_setup:
                    civ.set_mode("CW")
                    civ.set_break_in("SEMI")
                civ.set_cw_speed(args.wpm)
                secs = cw_duration(text, args.wpm)
                try:
                    civ.send_cw(text)
                    print(f"sending {text!r} at {args.wpm} WPM (~{secs:.1f}s) ...")
                    time.sleep(secs + 0.3)
                except KeyboardInterrupt:
                    civ.stop_cw()
                    print("stopped")
            elif args.action == "usbmod":
                if args.percent is None:
                    print(f"USB MOD Level: {civ.get_usb_mod_level()}%")
                else:
                    print(f"USB MOD Level set: {civ.set_usb_mod_level(args.percent)}%")
            elif args.action == "mode":
                civ.set_mode(args.mode, data=args.data, filt=args.filter)
                print(f"mode {args.mode}{'-D' if args.data else ''} FIL{args.filter}")
        except (CivError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
