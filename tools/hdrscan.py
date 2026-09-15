#!/usr/bin/env python3
"""
hdrscan -- поиск карт в заголовочной раскладке Bosch.

Часть карт в этой прошивке лежит не «голым телом плюс отдельная ось», а
одним куском вместе с осями:

    [nx][ny][ось X, nx значений][ось Y, ny значений][данные, nx*ny]

Так лежат KFLF (заголовок 0x190CE), KFMSNWDK (0x15664) и ещё несколько.
Указатель в коде ведёт на ЗАГОЛОВОК, поэтому поиск по адресу данных их не
находит -- именно на этом я один раз уже споткнулся.

Сканер перебирает сегмент и принимает место за заголовок, только если
сходится всё сразу:

  * nx и ny в разумных пределах;
  * обе оси строго возрастают (ось, которая не растёт, -- не ось);
  * весь блок целиком помещается в сегмент;
  * данные не вырождены -- не все одинаковые.

Этого достаточно, чтобы отсеять случайные совпадения: строгая
монотонность двух осей подряд на случайных данных почти не встречается.

    python3 tools/hdrscan.py firmware/FBH3ID60_stok.bin
    python3 tools/hdrscan.py firmware/FBH3ID60_stok.bin --width 2 --min-nx 6
"""

from __future__ import annotations

import argparse
import json
import os
import sys

DATA_LO, DATA_HI = 0x10000, 0x20000


def rd(fw: bytes, a: int, w: int) -> int:
    return fw[a] if w == 1 else int.from_bytes(fw[a:a + 2], "little")


def scan(fw: bytes, w: int, lo: int, hi: int, min_n: int, max_n: int,
         allow_flat: bool):
    step = w
    out = []
    for h in range(lo, hi - 8, 1 if w == 1 else 2):
        nx, ny = rd(fw, h, w), rd(fw, h + w, w)
        if not (min_n <= nx <= max_n and min_n <= ny <= max_n):
            continue
        ax0 = h + 2 * w
        ay0 = ax0 + nx * w
        d0 = ay0 + ny * w
        end = d0 + nx * ny * w
        if end > hi:
            continue
        ax = [rd(fw, ax0 + i * w, w) for i in range(nx)]
        ay = [rd(fw, ay0 + i * w, w) for i in range(ny)]
        if any(ax[i] >= ax[i + 1] for i in range(nx - 1)):
            continue
        if any(ay[i] >= ay[i + 1] for i in range(ny - 1)):
            continue
        dat = [rd(fw, d0 + i * w, w) for i in range(nx * ny)]
        if not allow_flat and len(set(dat)) < 3:
            continue
        out.append(dict(header=h, data=d0, nx=nx, ny=ny, width=w,
                        x_axis=ax, y_axis=ay,
                        dmin=min(dat), dmax=max(dat), end=end))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Карты в заголовочной раскладке Bosch")
    ap.add_argument("firmware")
    ap.add_argument("--width", type=int, choices=(1, 2, 0), default=0,
                    help="1 байт, 2 слово, 0 -- оба")
    ap.add_argument("--min-nx", type=int, default=4)
    ap.add_argument("--max-nx", type=int, default=20)
    ap.add_argument("--allow-flat", action="store_true",
                    help="не отбрасывать карты с одинаковыми значениями")
    ap.add_argument("--refs", help="JSON от calref.py -- пометить читаемые")
    ap.add_argument("--out")
    a = ap.parse_args(argv)

    fw = open(a.firmware, "rb").read()
    refs = set()
    if a.refs and os.path.exists(a.refs):
        refs = {int(k, 16) for k in json.load(open(a.refs))}

    widths = (1, 2) if a.width == 0 else (a.width,)
    found = []
    for w in widths:
        found += scan(fw, w, DATA_LO, DATA_HI, a.min_nx, a.max_nx,
                      a.allow_flat)
    # выбросить вложенные: если один заголовок лежит внутри другого блока
    found.sort(key=lambda f: (f["header"], -(f["end"] - f["header"])))
    keep, last_end = [], -1
    for f in found:
        if f["header"] < last_end:
            continue
        keep.append(f)
        last_end = f["end"]

    print("%-9s %-9s %-7s %-5s %-6s %s"
          % ("ЗАГОЛОВ", "ДАННЫЕ", "РАЗМЕР", "РАЗР", "ЧИТ", "ЗНАЧЕНИЯ"))
    for f in keep:
        mark = "да" if any(k in refs for k in
                           range(f["header"], f["header"] + 4)) else "--"
        print("0x%05X  0x%05X  %2dx%-4d %-5s %-6s %d..%d"
              % (f["header"], f["data"], f["nx"], f["ny"],
                 "u8" if f["width"] == 1 else "u16", mark,
                 f["dmin"], f["dmax"]))
    print("\nнайдено заголовочных карт: %d" % len(keep))
    if a.out:
        json.dump(keep, open(a.out, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print("записано " + a.out, file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
