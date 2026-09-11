#!/usr/bin/env python3
"""
calref -- кто из кода читает калибровочные ячейки.

Калибровка адресуется двумя способами, и искать надо оба.

1. Через DPP. Старшие два бита 16-битного адреса выбирают регистр DPP:

       mov r4, 0x4bb4        ; DPP1, смещение 0x0BB4 -> 0x814BB4 -> файл 0x14BB4

   В этой прошивке DPP0 = страница 0x204 (файл 0x10000..0x13FFF),
   DPP1 = 0x205 (файл 0x14000..0x17FFF). DPP2 и DPP3 -- ОЗУ и регистры,
   к калибровке отношения не имеют.

2. Через явную страницу, для верхней половины сегмента, куда DPP не
   смотрит:

       extp  0x206, #0x1     ; страница 0x206 -> 0x818000
       movbz r13, 0x04ee     ; смещение 0x4EE -> файл 0x184EE

Поэтому простой поиск 16-битного слова по коду не работает: во втором
случае адреса в коде нет вовсе, он собирается из двух инструкций.

    python3 tools/calref.py firmware/FBH3ID60_stok.bin --addr 0x14BB4
    python3 tools/calref.py firmware/FBH3ID60_stok.bin --range 0x14D00 0x15100
    python3 tools/calref.py firmware/FBH3ID60_stok.bin --out out/calref.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys

import c166dis

FLASH_BASE = 0x800000
DPP = {0: 0x204, 1: 0x205}          # страницы, на которые смотрят DPP0 и DPP1
HEX = re.compile(r"0x([0-9a-f]{1,4})\b")


def scan(data: bytes, lo: int, hi: int) -> dict[int, list[int]]:
    dis = c166dis.Disassembler()
    refs: dict[int, list[int]] = {}
    page = None
    page_left = 0

    def add(addr: int, site: int) -> None:
        if 0x10000 <= addr < len(data):
            refs.setdefault(addr, []).append(site)

    for ins in c166dis.disassemble(data, lo, hi, dis=dis):
        text = ins.mnem + " " + ", ".join(ins.ops)
        if ins.mnem in ("extp", "extpr"):
            m = re.match(r"(?:extp|extpr)\s+0x([0-9a-f]+),\s*#0x([0-9a-f]+)", text)
            page, page_left = (int(m.group(1), 16), int(m.group(2), 16)) if m else (None, 0)
            continue

        for m in HEX.finditer(text):
            val = int(m.group(1), 16)
            # непосредственные операнды (#0x...) адресами не считаем
            if m.start() and text[m.start() - 1] == "#":
                continue
            if page is not None and page_left > 0 and val < 0x4000:
                add(page * 0x4000 + val - FLASH_BASE, ins.addr)
            elif val < 0x8000:
                add(DPP[val >> 14] * 0x4000 + (val & 0x3FFF) - FLASH_BASE, ins.addr)

        if page_left > 0:
            page_left -= 1
            if page_left == 0:
                page = None
    return refs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Ссылки кода на калибровку")
    ap.add_argument("firmware")
    ap.add_argument("--code", nargs=2, type=lambda s: int(s, 0), default=[0x20000, 0x6C000])
    ap.add_argument("--addr", type=lambda s: int(s, 0), help="кто читает этот адрес")
    ap.add_argument("--range", nargs=2, type=lambda s: int(s, 0))
    ap.add_argument("--max-sites", type=int, default=6)
    ap.add_argument("--out")
    a = ap.parse_args(argv)

    data = open(a.firmware, "rb").read()
    refs = scan(data, a.code[0], a.code[1])
    print(f"различных калибровочных ячеек, читаемых кодом: {len(refs)}", file=sys.stderr)

    if a.addr is not None:
        s = refs.get(a.addr, [])
        print(f"0x{a.addr:05X}: обращений {len(s)}: "
              + ", ".join(f"0x{x:05X}" for x in s[:20]))
    if a.range:
        for addr in sorted(refs):
            if a.range[0] <= addr <= a.range[1]:
                s = refs[addr]
                print(f"  0x{addr:05X}  обращений {len(s):3d}: "
                      + ", ".join(f"0x{x:05X}" for x in s[:a.max_sites]))
    if a.out:
        json.dump({f"0x{k:05X}": [f"0x{v:05X}" for v in vs] for k, vs in sorted(refs.items())},
                  open(a.out, "w"), ensure_ascii=False, indent=1)
        print(f"записано {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
