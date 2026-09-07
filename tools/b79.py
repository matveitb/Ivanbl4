#!/usr/bin/env python3
"""
b79 -- чтение открытой части файлов разметки ChipTuningPRO (.b79) для M7.9.7.

Формат: сигнатура "CTM\\x04", дальше записи с полями по 32 байта. Строки
хранятся с префиксом длины (два байта, big-endian) и кодировкой cp1251.

Из файла читаются:

  * ОПРЕДЕЛЕНИЯ ОСЕЙ -- строки вида "!900/a14C12/n11/f0,0234375/o0/-w":
        a -- адрес в прошивке, n -- число точек, f -- множитель,
        o -- смещение, -w -- шестнадцатибитные точки (иначе байтовые);
  * ПОДПИСИ величин на русском ("УОЗ", "Фактор нагрузки, %", ...).

Чего в открытом виде НЕТ: адресов и размерностей самих карт. Они лежат в
бинарной части записи, формат которой разобрать не удалось. Кроме того,
эти файлы описывают прошивки Нивы, а не Kia, поэтому даже известные адреса
пришлось бы переносить, как это делается для DAMOS в tools/xfer.py.

Использование:
    python3 tools/b79.py firmware/*.b79
    python3 tools/b79.py firmware/B103EQ07.b79 --labels
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass

MAGIC = b"CTM\x04"
AXIS_RE = re.compile(rb'![0-9]+((?:/[a-z-][^/\x00\s]*)+)')


@dataclass
class Axis:
    addr: int
    points: int
    factor: float
    offset: float
    width: int          # 1 или 2 байта на точку
    raw: str

    def describe(self) -> str:
        s = "0x%05X  %2d точек  u%d  x%g" % (self.addr, self.points,
                                             self.width * 8, self.factor)
        if self.offset:
            s += " %+g" % self.offset
        return s


def _num(s: str) -> float:
    return float(s.replace(",", "."))


def parse_axes(data: bytes) -> list[Axis]:
    out = []
    for m in AXIS_RE.finditer(data):
        s = m.group().decode("cp1251", errors="replace")
        a = re.search(r"/a([0-9A-Fa-f]+)", s)
        n = re.search(r"/n(\d+)", s)
        f = re.search(r"/f([-\d,\.]+)", s)
        o = re.search(r"/o(-?[\d,\.]+)", s)
        if not (a and n and f):
            continue
        out.append(Axis(addr=int(a.group(1), 16), points=int(n.group(1)),
                        factor=_num(f.group(1)),
                        offset=_num(o.group(1)) if o else 0.0,
                        width=2 if "-w" in s else 1, raw=s))
    return out


def parse_labels(data: bytes) -> list[str]:
    """Строки с префиксом длины (BE u16) в 32-байтовых слотах."""
    out, a = [], 0
    while a < len(data) - 2:
        n = (data[a] << 8) | data[a + 1]
        if 1 <= n <= 30 and a + 32 <= len(data):
            s = data[a + 2:a + 2 + n]
            if all(0x20 <= c < 0x7F or 0xC0 <= c <= 0xFF for c in s) \
                    and data[a + 2 + n:a + 32].count(0) == 30 - n:
                out.append(s.decode("cp1251", errors="replace"))
                a += 32
                continue
        a += 1
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Чтение разметки ChipTuningPRO (.b79)")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--labels", action="store_true", help="показать подписи величин")
    args = ap.parse_args(argv)

    seen: dict[tuple, set] = {}
    labels: set[str] = set()
    for p in args.files:
        data = open(p, "rb").read()
        if not data.startswith(MAGIC):
            print("%s: не похоже на .b79 (нет сигнатуры CTM)" % p, file=sys.stderr)
            continue
        name = os.path.basename(p)
        ax = parse_axes(data)
        for a in ax:
            seen.setdefault((a.addr, a.points, a.factor, a.offset, a.width),
                            set()).add(name[:8])
        for t in parse_labels(data):
            if sum(1 for c in t if ord(c) > 127) >= 2:
                labels.add(t)
        print("%-22s осей: %-4d подписей: %d" % (name, len(ax), len(parse_labels(data))),
              file=sys.stderr)

    print("\n%-9s %-7s %-5s %-12s %-8s %s" %
          ("АДРЕС", "ТОЧЕК", "РАЗР", "МНОЖИТЕЛЬ", "СМЕЩ", "ФАЙЛЫ"))
    for k in sorted(seen):
        addr, pts, fac, off, w = k
        print("0x%05X  %-7d %-5s %-12g %-8g %s"
              % (addr, pts, "u%d" % (w * 8), fac, off, ",".join(sorted(seen[k]))))
    print("\nРазных определений осей: %d" % len(seen), file=sys.stderr)

    if args.labels:
        print("\nПодписи величин (%d):" % len(labels))
        for t in sorted(labels):
            print("   ", t)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
