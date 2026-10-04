#!/usr/bin/env python3
"""
orient -- какая ось у двумерной карты идёт ПОДРЯД, по коду.

Процедура интерполяции карты получает, кроме указателя на данные, указатель
на ЗАГОЛОВОК: счётчик точек той оси, значения которой лежат в памяти
подряд (по нему считается шаг строки). Так вызываются

    0x833FFC   r12/r13 -- данные (со страницей), r14/r15 -- заголовок;
    0x0078B8,
    0x007856   r12 -- данные, r13 -- заголовок (без страницы).

Значит, число точек подряд у каждой такой карты доказано кодом, и его
можно сверить с тем, как карта объявлена в A2L. Без этой сверки две ошибки
прошли незамеченными: плёночная модель и KFFDLBTS читались поперёк, потому
что поле rows в профиле записывали то в одном смысле, то в другом.

    python3 tools/orient.py --a2l results/FBH3ID60_legacy_all.a2l firmware/FBH3ID60_stok.bin
"""

from __future__ import annotations

import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "editor", "a2l"))
sys.path.insert(0, os.path.join(ROOT, "editor", "core"))

import c166dis                                              # noqa: E402

FLASH = 0x800000
PAGED = {"0x833ffc"}
UNPAGED = {"0x0078b8", "0x007856"}


def cal(v: int, page) -> int | None:
    if page == 0x206:
        return 0x18000 + v if v < 0x4000 else None
    if page == 0x205:
        return 0x14000 + v if v < 0x4000 else None
    if page is None and v < 0x10000:
        return 0x10000 + v
    return None


def calls(data: bytes, lo: int = 0x20000, hi: int | None = None) -> list:
    """[(место, процедура, данные, заголовок)] по всем вызовам карт."""
    hi = hi or len(data)
    dis = c166dis.Disassembler()
    regs: dict = {}
    out = []
    off = lo
    while off < hi:
        ins = dis.decode(data, off, off + FLASH)
        if ins.length <= 0:
            break
        mn, ops = ins.mnem, ins.ops
        site = off + FLASH
        if mn == "mov" and len(ops) == 2 and ops[1].startswith("#"):
            m = re.match(r"r(\d+)$", ops[0])
            if m:
                try:
                    regs[int(m.group(1))] = (int(ops[1][1:], 0), site)
                except ValueError:
                    pass
        elif mn in ("calls", "callr", "calla") and ops:
            tgt = ops[-1].lower()
            fresh = {r: v for r, (v, s) in regs.items() if site - s < 48}
            if tgt in PAGED and all(r in fresh for r in (12, 13, 14, 15)):
                d, h = cal(fresh[12], fresh[13]), cal(fresh[14], fresh[15])
                if d and h:
                    out.append((site, tgt, d, h))
            elif tgt in UNPAGED and 12 in fresh and 13 in fresh:
                out.append((site, tgt, cal(fresh[12], None), cal(fresh[13], None)))
            regs = {}
        elif mn in ("ret", "rets", "reti", "jmps", "jmpa"):
            if mn != "jmpa" or "cc_UC" in ins.text():
                regs = {}
        off += ins.length
    return out


def main(argv=None) -> int:
    import geometry                                         # noqa: E402
    import model                                            # noqa: E402

    ap = argparse.ArgumentParser(description="ориентация карт по коду")
    ap.add_argument("firmware")
    ap.add_argument("--a2l", default=os.path.join(ROOT, "results", "FBH3ID60_legacy.a2l"))
    a = ap.parse_args(argv)
    data = open(a.firmware, "rb").read()
    a2l = model.load(a.a2l)
    lay = geometry.resolve_all(a2l, data, geometry.detect_addressing(a2l, len(data)))
    by_data = {L.data_off: (n, L) for n, L in lay.items()}
    bad = ok = square = 0
    for site, tgt, d, h in calls(data):
        if d not in by_data:
            continue
        n, L = by_data[d]
        cnt = data[h]
        if L.ny <= 1:
            continue
        if L.nx == L.ny:
            square += 1
            continue
        if cnt == L.nx:
            ok += 1
        else:
            bad += 1
            print("ПОПЕРЁК  %-24s 0x%05X  объявлено %d подряд на %d строк, "
                  "а заголовок 0x%05X = %d  (0x%06X %s)"
                  % (n, d, L.nx, L.ny, h, cnt, site, tgt))
    print("сошлось: %d, поперёк: %d, квадратных (не различить): %d" % (ok, bad, square))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
