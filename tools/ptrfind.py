#!/usr/bin/env python3
"""
ptrfind -- найти в коде указатель на калибровочный адрес.

Половина находок держится на сдвиге области и здравом смысле. Чтобы
поднять их до подтверждённых, нужна ссылка из кода. Но искать адрес
как число бесполезно: код работает с 16-битными указателями плюс
страницей DPP или extp, и одному файловому смещению соответствует
несколько возможных форм записи.

Наблюдённые соответствия (проверены на KFLF, KFMSNWDK, KFPU, KFRLW):

    0x10000-0x13FFF  ->  addr - 0x10000   (DPP0, страница 0x204)
    0x14000-0x17FFF  ->  addr - 0x10000   (DPP1, страница 0x205)
    0x18000-0x1BFFF  ->  addr - 0x18000   (страница 0x206)
    0x1C000-0x1FFFF  ->  addr - 0x18000   (страница 0x207)

Кроме непосредственной загрузки указателя (`mov r12, #0x1485`) бывает и
прямое чтение ячейки (`mov r12, 0x2c76`), когда код сам разбирает
заголовок карты. Ищутся обе формы, плюс адрес на единицу меньше -- у
кривых с ведущим счётчиком указатель часто ведёт на счётчик.

    python3 tools/ptrfind.py firmware/FBH3ID60_stok.bin 0x11580 0x15B40
"""

from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import c166dis                                              # noqa: E402


def forms(addr: int) -> list[int]:
    """Возможные 16-битные записи файлового смещения."""
    out = set()
    if 0x10000 <= addr < 0x18000:
        out.add(addr - 0x10000)
    if 0x18000 <= addr < 0x20000:
        out.add(addr - 0x18000)
        out.add(addr - 0x14000)          # страница 0x207 как 0x4000+
    out.add(addr & 0x3FFF)
    out.add((addr & 0x3FFF) | 0x4000)
    return sorted(v for v in out if 0 <= v <= 0xFFFF)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Указатели на калибровку в коде")
    ap.add_argument("firmware")
    ap.add_argument("addr", nargs="+")
    ap.add_argument("--range", nargs=2, default=("0x20000", "0x80000"))
    ap.add_argument("--slack", type=int, default=1,
                    help="искать также адреса на N меньше (ведущий счётчик)")
    a = ap.parse_args(argv)

    fw = open(a.firmware, "rb").read()
    lo, hi = int(a.range[0], 0), int(a.range[1], 0)

    want: dict[int, list[int]] = {}
    for s in a.addr:
        base = int(s, 0)
        for off in range(-a.slack, 1):
            for f in forms(base + off):
                want.setdefault(f, []).append(base)

    pat = re.compile(r"(?:#)?0x([0-9a-f]{1,4})\b")
    hits: dict[int, list] = {}
    dis = c166dis.Disassembler()
    for ins in c166dis.disassemble(fw, lo, hi, base=0x800000, dis=dis):
        t = " ".join(ins.text().split())
        for m in pat.finditer(t):
            v = int(m.group(1), 16)
            if v in want:
                for tgt in want[v]:
                    hits.setdefault(tgt, []).append((ins.addr, t))

    for s in a.addr:
        base = int(s, 0)
        got = hits.get(base, [])
        print("\n=== 0x%05X: ссылок %d  (формы: %s) ==="
              % (base, len(got),
                 ", ".join("0x%X" % f for f in forms(base))))
        seen = set()
        for addr, t in got:
            if addr in seen:
                continue
            seen.add(addr)
            print("  %06X  %s" % (addr, t))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
