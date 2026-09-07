#!/usr/bin/env python3
"""
fullload -- чтение и правка KFLBTS, заданной лямбды на полной нагрузке
(обогащение защиты компонентов) в прошивках FBH3ID60 / Bosch M7.9.7.

    адрес  0x19636, 12 строк (нагрузка) x 16 столбцов (обороты), u8
    физика lambda = байт * 0.0078125

Оси -- из цепочки 0x181C1:
    X (обороты) SNM16GKUB 0x181F7, 16 точек, x40
    Y (нагрузка) SRL12GKUB 0x1821E, 12 точек, x0.75

Показать карту:
    python3 tools/fullload.py show firmware/FBH3ID60_stok.bin

Залить lambda 0.92 в зону низов при полном газе (строки 7..11, столбцы 3..7):
    python3 tools/fullload.py set firmware/FBH3ID60_stok.bin -o out/stock_ll.bin \
        --lam 0.92 --rows 7:11 --cols 3:8

После правки контрольную сумму пересчитывает tools/bosch_csum.py.
"""

from __future__ import annotations

import argparse
import sys

ADDR = 0x19636
ROWS, COLS = 12, 16
FACTOR = 0.0078125
AX_N = 0x181F7          # обороты, счётчик + 16 байт, x40
AX_L = 0x1821E          # нагрузка, счётчик + 12 байт, x0.75


def axes(data: bytes) -> tuple[list[int], list[float]]:
    n = [data[AX_N + 1 + i] * 40 for i in range(data[AX_N])]
    l = [data[AX_L + 1 + i] * 0.75 for i in range(data[AX_L])]
    return n, l


def show(data: bytes) -> None:
    n, l = axes(data)
    print("нагр\\об |" + "".join(f"{v:>6.0f}" for v in n))
    for r in range(ROWS):
        row = "".join(f"{data[ADDR + r * COLS + c] * FACTOR:>6.2f}" for c in range(COLS))
        print(f"{l[r]:7.2f} |{row}")


def _range(s: str, limit: int) -> range:
    a, _, b = s.partition(":")
    lo = int(a)
    hi = int(b) if b else lo
    if not (0 <= lo <= hi < limit):
        raise SystemExit(f"диапазон {s} вне 0..{limit - 1}")
    return range(lo, hi + 1)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="KFLBTS: лямбда на полной нагрузке")
    ap.add_argument("action", choices=["show", "set"])
    ap.add_argument("firmware")
    ap.add_argument("-o", "--out", help="куда записать результат (для set)")
    ap.add_argument("--lam", type=float, help="целевая лямбда, например 0.92")
    ap.add_argument("--rows", default="7:11", help="строки нагрузки, например 7:11")
    ap.add_argument("--cols", default="3:7", help="столбцы оборотов, например 3:7")
    ap.add_argument("--only-richer", action="store_true",
                    help="не трогать ячейки, которые уже богаче цели")
    a = ap.parse_args(argv)

    data = bytearray(open(a.firmware, "rb").read())
    if a.action == "show":
        show(bytes(data))
        return 0

    if a.lam is None or not a.out:
        raise SystemExit("для set нужны --lam и -o")
    raw = round(a.lam / FACTOR)
    if not (0 <= raw <= 255):
        raise SystemExit("лямбда вне диапазона байта")

    changed = 0
    for r in _range(a.rows, ROWS):
        for c in _range(a.cols, COLS):
            off = ADDR + r * COLS + c
            if a.only_richer and data[off] <= raw:
                continue
            if data[off] != raw:
                data[off] = raw
                changed += 1
    open(a.out, "wb").write(bytes(data))
    print(f"lambda {raw * FACTOR:.4f} (байт {raw}), изменено ячеек: {changed}")
    print(f"записано {a.out}; пересчитайте сумму: "
          f"python3 tools/bosch_csum.py fix {a.out} -o {a.out}")
    show(bytes(data))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:      # вывод обрезан через head и подобное
        sys.exit(0)
