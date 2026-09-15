#!/usr/bin/env python3
"""
torque -- моментная кривая по модели ЭБУ.

Собирает вместе всё, что найдено в моментной модели, и считает то, что
блок считает сам:

    момент на валу = (KFMIOP(n, rl) - KFMDS(n, rl)) * MDNORM / 100

Нагрузка rl берётся не «сто процентов», а настоящая для полного газа --
из RLVMXN, где она 79..92 % и до сотни не доходит никогда.

MDNORM в этой сборке не калибровка: её нет ни в дамосе, ни среди
непосредственных значений в моментном участке кода. Поэтому она здесь не
задаётся, а ПОДБИРАЕТСЯ под известный паспортный момент. Заодно это
проверка самой модели: если обороты расчётного максимума совпадут с
паспортными, значит карты собраны верно.

    python3 tools/torque.py firmware/FBH3ID60_stok.bin --peak 143 --at 4500
"""

from __future__ import annotations

import argparse
import sys

KFMIOP, KFMIOP_NX, KFMIOP_NY = 0x14A10, 16, 11
AX_RPM, AX_RL = 0x100F2, 0x14B70
KFMDS_HDR = 0x15112
RLVMXN = 0x182B6
F_PCT = 100 / 65536


def rd16(fw, a):
    return int.from_bytes(fw[a:a + 2], "little")


def interp(xs, ys, x):
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    for i in range(len(xs) - 1):
        if xs[i] <= x <= xs[i + 1]:
            t = (x - xs[i]) / (xs[i + 1] - xs[i])
            return ys[i] + t * (ys[i + 1] - ys[i])
    return ys[-1]


def grid2(xs, ys, cells, x, y):
    """Двумерная линейная интерполяция: cells[i][j] по xs[i], ys[j]."""
    col = [interp(ys, row, y) for row in cells]
    return interp(xs, col, x)


def load_all(fw):
    rpm = [fw[AX_RPM + 1 + i] * 40 for i in range(KFMIOP_NX)]
    n = rd16(fw, AX_RL)
    rl = [rd16(fw, AX_RL + 2 + i * 2) * 0.0234375 for i in range(n)]
    mi = [[rd16(fw, KFMIOP + (r * KFMIOP_NY + c) * 2) * F_PCT
           for c in range(KFMIOP_NY)] for r in range(KFMIOP_NX)]

    H = KFMDS_HDR
    dnx, dny = rd16(fw, H), rd16(fw, H + 2)
    drpm = [rd16(fw, H + 4 + i * 2) * 0.25 for i in range(dnx)]
    drl = [rd16(fw, H + 4 + dnx * 2 + i * 2) * 0.0234375 for i in range(dny)]
    d0 = H + 4 + dnx * 2 + dny * 2
    ds = [[rd16(fw, d0 + (r * dny + c) * 2) * F_PCT for c in range(dny)]
          for r in range(dnx)]

    k = fw[RLVMXN]
    vrpm = [fw[RLVMXN + 1 + i] * 40 for i in range(k)]
    vrl = [fw[RLVMXN + 1 + k + i] * 0.75 for i in range(k)]
    return rpm, rl, mi, drpm, drl, ds, vrpm, vrl


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Моментная кривая по модели ЭБУ")
    ap.add_argument("firmware")
    ap.add_argument("--peak", type=float, default=143.0,
                    help="паспортный максимальный момент, Нм")
    ap.add_argument("--at", type=float, default=4500.0,
                    help="обороты паспортного максимума")
    a = ap.parse_args(argv)

    fw = open(a.firmware, "rb").read()
    rpm, rl, mi, drpm, drl, ds, vrpm, vrl = load_all(fw)

    print("%-8s %-9s %-9s %-9s %s"
          % ("об/мин", "rl %", "KFMIOP %", "KFMDS %", "чистый %"))
    rows = []
    for n in range(800, 6600, 200):
        r = interp(vrpm, vrl, n)
        m = grid2(rpm, rl, mi, n, r)
        d = grid2(drpm, drl, ds, n, r)
        rows.append((n, r, m, d, m - d))
    for n, r, m, d, net in rows:
        if n % 400 == 0:
            print("%-8d %-9.2f %-9.2f %-9.2f %.2f" % (n, r, m, d, net))

    best = max(rows, key=lambda t: t[4])
    print("\nмаксимум чистого момента модели: %.2f %% на %.0f об/мин"
          % (best[4], best[0]))
    mdn = a.peak / best[4] * 100
    print("если паспортный максимум %.0f Нм, то MDNORM = %.0f Нм"
          % (a.peak, mdn))
    print("расхождение расчётных оборотов максимума с паспортными "
          "(%.0f): %.0f об/мин" % (a.at, abs(best[0] - a.at)))

    print("\nмоментная кривая при MDNORM = %.0f Нм:" % mdn)
    print("%-8s %s" % ("об/мин", "Нм"))
    for n, r, m, d, net in rows:
        if n % 400 == 0:
            print("%-8d %.1f" % (n, net * mdn / 100))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
