#!/usr/bin/env python3
"""
coverage -- сколько правок тюнеров попадает в наши карты.

Честная мера качества разметки, и мера единственная, которая не зависит
от чужого мнения. Логика простая: всё, что человек правил в чужой
прошивке, он правил ОСМЫСЛЕННО, значит по этому адресу стоит настоящая
калибровка. Если наш A2L её не знает -- это дыра, и видно, где именно.

Обратное неверно: попадание в карту не доказывает, что у карты верное
ИМЯ. Проверяется покрытие, а не подписи.

    python3 tools/coverage.py --a2l results/FBH3ID60_legacy.a2l \
        --stock firmware/FBH3ID60_stok.bin --tuned firmware/*.bin
"""

from __future__ import annotations

import argparse
import bisect
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "editor", "a2l"))
sys.path.insert(0, os.path.join(ROOT, "editor", "core"))

import geometry                                             # noqa: E402
import model                                                # noqa: E402

# Не карты, и правки тут ничего не говорят о разметке.
SKIP = (
    (0x10000, 0x10030, "область идентификации"),
    (0x1FC00, 0x1FD00, "таблица контрольных сумм"),
    (0x127AA, 0x127AC, "указатель на подпрограмму (снятие иммобилайзера)"),
)


def _auto_int(s: str) -> int:
    return int(s, 0)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Покрытие правок картами")
    ap.add_argument("--a2l", required=True)
    ap.add_argument("--stock", required=True)
    ap.add_argument("--tuned", nargs="+", required=True)
    ap.add_argument("--range", nargs=2, type=_auto_int,
                    default=(0x10000, 0x20000), metavar=("START", "END"))
    ap.add_argument("--max-diff", type=int, default=4000,
                    help="больше -- значит это другая прошивка, а не правка")
    a = ap.parse_args(argv)

    stock = open(a.stock, "rb").read()
    a2l = model.load(a.a2l)
    am = geometry.detect_addressing(a2l, len(stock))
    lay = geometry.resolve_all(a2l, stock, am)

    spans = sorted((L.data_off, L.end, L.name) for L in lay.values() if L.size)
    starts = [s[0] for s in spans]
    widest = max((e - s for s, e, _ in spans), default=0)

    def owner(addr):
        i = bisect.bisect_right(starts, addr) - 1
        while i >= 0 and spans[i][0] > addr - widest:
            if spans[i][0] <= addr < spans[i][1]:
                return spans[i][2]
            i -= 1
        return None

    def skipped(addr):
        for lo, hi, why in SKIP:
            if lo <= addr < hi:
                return why
        return None

    total = hit = 0
    miss: dict = {}
    print("%-46s %7s %7s %6s" % ("ПРОШИВКА", "ПРАВОК", "В КАРТАХ", "ДОЛЯ"))
    for path in sorted(a.tuned):
        if os.path.abspath(path) == os.path.abspath(a.stock):
            continue
        d = open(path, "rb").read()
        if len(d) != len(stock):
            continue
        ch = [x for x in range(*a.range)
              if d[x] != stock[x] and not skipped(x)]
        if not ch or len(ch) > a.max_diff:
            continue
        good = sum(1 for x in ch if owner(x))
        total += len(ch)
        hit += good
        for x in ch:
            if not owner(x):
                miss.setdefault(x, set()).add(os.path.basename(path)[:18])
        print("%-46s %7d %7d %5.0f %%"
              % (os.path.basename(path)[:46], len(ch), good,
                 100.0 * good / len(ch)))

    if not total:
        print("\nНечего сравнивать.")
        return 1
    print("\nИТОГО: %d правленых байт, в известных картах %d (%.1f %%)"
          % (total, hit, 100.0 * hit / total))

    runs = []
    prev = None
    for x in sorted(miss):
        if prev is not None and x == prev + 1:
            runs[-1][1] = x
        else:
            runs.append([x, x])
        prev = x
    print("Не опознано: %d байт в %d участках" % (len(miss), len(runs)))
    for lo, hi in runs:
        print("   0x%05X..0x%05X (%3d б)  %s"
              % (lo, hi, hi - lo + 1,
                 ", ".join(sorted(miss[lo]))[:56]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
