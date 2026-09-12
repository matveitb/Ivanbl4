#!/usr/bin/env python3
"""
advance -- добавка к карте оптимального угла KFZWOP.

Смысл правки: поднимается ПОТОЛОК, до которого контуру детонации
разрешено дойти, а базовая карта KFZW не трогается. Контур стартует от
базовой и сам идёт вверх к оптимуму, пока не услышит стук. Терпит мотор --
заберёт прибавку, не терпит -- откатится, и хуже не станет.

    KFZWOP 0x10529, 16 строк оборотов x 11 столбцов нагрузки,
    знаковый байт, шаг 0.75 град.
    ось оборотов  0x100F2 (счётчик + 16 байт, x40)
    ось нагрузки  0x14B70 (счётчик-слово + 11 слов, x0.0234375)

Шаг сетки 0.75 град, поэтому произвольные значения не выставить:
ближайшие к "плюс два" -- 1.5 и 2.25.

    python3 tools/advance.py "firmware/FBH3ID60 e2 tun csok v2___.bin" \\
        -o out/x.bin --delta 2.25 --from-rpm 2000 --from-load 50

После правки контрольную сумму пересчитывает tools/bosch_csum.py.
"""

from __future__ import annotations

import argparse
import sys

KFZWOP = 0x10529
COLS = 11
AX_N = 0x100F2          # обороты: счётчик-байт + n байт, x40
AX_L = 0x14B70          # нагрузка: счётчик-слово + n слов, x0.0234375
STEP = 0.75


def axes(d: bytes) -> tuple[list[int], list[float]]:
    n = [d[AX_N + 1 + i] * 40 for i in range(d[AX_N])]
    cnt = d[AX_L] | (d[AX_L + 1] << 8)
    l = [(d[AX_L + 2 + 2 * i] | (d[AX_L + 3 + 2 * i] << 8)) * 0.0234375 for i in range(cnt)]
    return n, l


def deg(raw: int) -> float:
    return (raw - 256 if raw > 127 else raw) * STEP


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Добавка к KFZWOP")
    ap.add_argument("firmware")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--delta", type=float, default=2.25, help="прибавка в градусах, кратна 0.75")
    ap.add_argument("--from-rpm", type=float, default=2000.0)
    ap.add_argument("--from-load", type=float, default=50.0)
    ap.add_argument("--max-angle", type=float, default=40.0,
                    help="не поднимать выше этого значения")
    a = ap.parse_args(argv)

    units = a.delta / STEP
    if abs(units - round(units)) > 1e-6:
        raise SystemExit(f"прибавка {a.delta} не кратна шагу {STEP}; "
                         f"ближайшие: {round(units) * STEP} и {(round(units) + 1) * STEP}")
    units = int(round(units))

    data = bytearray(open(a.firmware, "rb").read())
    rpm, load = axes(bytes(data))
    rows = [i for i, v in enumerate(rpm) if v >= a.from_rpm]
    cols = [i for i, v in enumerate(load) if v >= a.from_load]
    print(f"прибавка {a.delta:+.2f}° ({units:+d} ед.), "
          f"обороты от {rpm[rows[0]]:.0f}, нагрузка от {load[cols[0]]:.0f} %")
    print(f"{'об/мин':>7} |" + "".join(f"{load[c]:>7.0f}" for c in cols))
    changed = capped = 0
    for r in rows:
        out = []
        for c in cols:
            off = KFZWOP + r * COLS + c
            old = deg(data[off])
            new = old + a.delta
            if new > a.max_angle:
                new = old
                capped += 1
            if new != old:
                data[off] = round(new / STEP) & 0xFF
                changed += 1
            out.append(f"{old:>6.2f}>{deg(data[off]):<6.2f}")
        print(f"{rpm[r]:7.0f} |" + " ".join(out))
    print(f"\nизменено ячеек: {changed}" + (f", упёрлось в потолок {a.max_angle}°: {capped}" if capped else ""))
    open(a.out, "wb").write(bytes(data))
    print(f"записано {a.out}; пересчитайте сумму: "
          f"python3 tools/bosch_csum.py fix {a.out} -o <новый>")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
