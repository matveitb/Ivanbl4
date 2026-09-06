#!/usr/bin/env python3
"""
showmap -- показать карту из профиля в одной или нескольких прошивках,
с матрицей разностей.

    python3 tools/showmap.py --profile profiles/FBH3ID60.json --name IGN_MAIN_11x16 \
        firmware/stok.bin "firmware/csok.bin" "firmware/csok v2.bin"

    python3 tools/showmap.py --addr 0x10529 --nx 11 --ny 16 fw.bin другая.bin

Опция --scale печатает вторую таблицу в физических единицах
(например, --scale 0.75 --unit "град" для угла опережения зажигания).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import fwlib


def _int(v) -> int:
    return v if isinstance(v, int) else int(str(v), 0)


def find_in_profile(path: str, name: str) -> dict:
    prof = json.load(open(path, encoding="utf-8"))
    for m in prof.get("maps", []):
        if m.get("name") == name:
            return m
    raise SystemExit("Карта %r не найдена в профиле. Доступны: %s" %
                     (name, ", ".join(m.get("name", "?") for m in prof.get("maps", []))))


def read_map(fw: fwlib.Firmware, data_addr: int, nx: int, ny: int, dw: int):
    flat = fw.vec(data_addr, nx * ny, dw)
    return fwlib.reshape(flat, ny, nx)


def fmt(matrix, width=5, scale=None):
    lines = []
    for row in matrix:
        if scale:
            lines.append(" ".join(("%%%d.1f" % width) % (v * scale) for v in row))
        else:
            lines.append(" ".join(("%%%dd" % width) % v for v in row))
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Показать карту в прошивках")
    ap.add_argument("firmwares", nargs="+")
    ap.add_argument("--profile")
    ap.add_argument("--name")
    ap.add_argument("--addr", type=_int)
    ap.add_argument("--data-addr", type=_int)
    ap.add_argument("--nx", type=int)
    ap.add_argument("--ny", type=int, default=1)
    ap.add_argument("--dw", type=int, default=1, help="разрядность данных в байтах")
    ap.add_argument("--scale", type=float, help="множитель для физических единиц")
    ap.add_argument("--unit", default="", help="подпись единиц")
    args = ap.parse_args(argv)

    if args.profile and args.name:
        m = find_in_profile(args.profile, args.name)
        addr = _int(m["addr"])
        data_addr = _int(m.get("data_addr", addr))
        nx, ny = int(m["nx"]), int(m.get("ny", 1))
        dw = int(m.get("data_width", 1))
        title = m["name"]
        note = m.get("note", "")
    elif args.addr is not None and args.nx:
        addr = args.addr
        data_addr = args.data_addr if args.data_addr is not None else addr
        nx, ny, dw = args.nx, args.ny, args.dw
        title = "карта @0x%X" % addr
        note = ""
    else:
        print("Укажите --profile/--name или --addr/--nx", file=sys.stderr)
        return 2

    print("=" * 70)
    print("%s   %dx%d, u%d, данные @0x%X" % (title, nx, ny, dw * 8, data_addr))
    if note:
        print(note)
    print("=" * 70)

    mats = []
    for path in args.firmwares:
        fw = fwlib.load(path)
        mat = read_map(fw, data_addr, nx, ny, dw)
        mats.append((os.path.basename(path), mat))
        print("\n--- %s ---" % os.path.basename(path))
        print(fmt(mat))
        if args.scale:
            print("  (в физ. единицах%s)" % (", " + args.unit if args.unit else ""))
            print(fmt(mat, scale=args.scale))

    for i in range(1, len(mats)):
        na, ma = mats[0]
        nb, mb = mats[i]
        print("\n--- разность: %s - %s ---" % (nb, na))
        diff = [[b - a for a, b in zip(ra, rb)] for ra, rb in zip(ma, mb)]
        print(fmt(diff))
        flat = [v for r in diff for v in r]
        changed = sum(1 for v in flat if v)
        print("изменено ячеек: %d из %d, диапазон [%+d..%+d]"
              % (changed, len(flat), min(flat), max(flat)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
