#!/usr/bin/env python3
"""
ramref -- кто в коде читает и пишет данную ячейку ОЗУ.

calref отвечает на вопрос «кто читает эту калибровку». Но половина
работы -- проследить не калибровку, а ВЕЛИЧИНУ: откуда она берётся и
куда уходит. Цикловое наполнение лежит в ОЗУ 0x9368, расход на оборот --
в 0xF8A0, результат АЦП -- в 0xF7B2. Пока не видно, кто их пишет, тракт
не прослежен.

Разделение на чтение и запись делается по позиции операнда: в `mov
приёмник, источник` запись -- это когда адрес стоит первым.

    python3 tools/ramref.py firmware/FBH3ID60_stok.bin --addr 0x9368
    python3 tools/ramref.py firmware/FBH3ID60_stok.bin --addr 0x9368 --context 6
"""

from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import c166dis                                              # noqa: E402


def scan(fw: bytes, targets: set[int], lo: int, hi: int):
    """Вернуть [(адрес_кода, текст, это_запись)] по всем целям."""
    pats = {a: re.compile(r"\b0x%04x\b" % a) for a in targets}
    out = []
    dis = c166dis.Disassembler()
    for ins in c166dis.disassemble(fw, lo, hi, base=0x800000, dis=dis):
        t = ins.text()
        for a, rx in pats.items():
            m = rx.search(t)
            if not m:
                continue
            # операнды идут после мнемоники; запись -- если цель первая
            ops = t.split(None, 1)[1] if " " in t else ""
            first = ops.split(",")[0].strip() if ops else ""
            out.append((ins.addr, t, first == m.group(0), a))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Ссылки кода на ячейку ОЗУ")
    ap.add_argument("firmware")
    ap.add_argument("--addr", action="append", required=True,
                    help="адрес ОЗУ, можно несколько")
    ap.add_argument("--range", nargs=2, default=("0x20000", "0x80000"))
    ap.add_argument("--context", type=int, default=0,
                    help="сколько инструкций показать вокруг каждой ссылки")
    a = ap.parse_args(argv)

    fw = open(a.firmware, "rb").read()
    targets = {int(x, 0) for x in a.addr}
    lo, hi = int(a.range[0], 0), int(a.range[1], 0)
    hits = scan(fw, targets, lo, hi)

    for tgt in sorted(targets):
        sel = [h for h in hits if h[3] == tgt]
        w = [h for h in sel if h[2]]
        r = [h for h in sel if not h[2]]
        print("\n=== 0x%04X: записей %d, чтений %d ===" % (tgt, len(w), len(r)))
        for addr, text, is_w, _ in sel:
            print("  %06X  %-6s %s" % (addr, "ЗАПИСЬ" if is_w else "чтение", text))
            if a.context:
                dis = c166dis.Disassembler()
                start = max(lo, addr - 0x800000 - a.context * 4)
                for ins in c166dis.disassemble(
                        fw, start, addr - 0x800000 + a.context * 4,
                        base=0x800000, dis=dis):
                    mark = ">>" if ins.addr == addr else "  "
                    print("       %s %06X  %s" % (mark, ins.addr, ins.text()))
                print()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
