#!/usr/bin/env python3
"""
hexdump -- шестнадцатеричный дамп участка прошивки с подсветкой отличий.

    python3 tools/hexdump.py fw.bin 0x10500 0x100
    python3 tools/hexdump.py a.bin 0x10500 0x100 --diff b.bin
    python3 tools/hexdump.py fw.bin 0x10500 0x100 --width 16 --dec
"""
from __future__ import annotations
import argparse
import fwlib


def _auto_int(s: str) -> int:
    return int(s, 0)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Дамп участка прошивки")
    ap.add_argument("firmware")
    ap.add_argument("addr", type=_auto_int)
    ap.add_argument("length", type=_auto_int, nargs="?", default=0x100)
    ap.add_argument("--width", type=int, default=16)
    ap.add_argument("--diff", help="второй файл: отличающиеся байты помечаются '*'")
    ap.add_argument("--dec", action="store_true", help="печатать десятичные значения")
    args = ap.parse_args(argv)

    fw = fwlib.load(args.firmware)
    other = fwlib.load(args.diff) if args.diff else None

    a, n, w = args.addr, args.length, args.width
    for row in range(a, min(a + n, len(fw)), w):
        chunk = fw.data[row:row + w]
        marks = []
        for i, b in enumerate(chunk):
            off = row + i
            changed = other is not None and off < len(other) and other.data[off] != b
            if args.dec:
                marks.append(("%3d" % b) + ("*" if changed else " "))
            else:
                marks.append(("%02X" % b) + ("*" if changed else " "))
        ascii_ = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in chunk)
        print("%s  %s |%s|" % (fwlib.hexa(row), "".join(marks), ascii_))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
