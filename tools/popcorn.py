#!/usr/bin/env python3
"""
popcorn -- поздний угол в углу карты для отстрелов при сбросе газа.

Идея правки: окно по оборотам задаёт карта зажигания, а не отсечка
топлива. Отсечка в M7.9.7 устроена как «выше порога режем, ниже
возвращаем», окна в ней нет. Поэтому отсечка выключается целиком, а
поздний угол ставится только в нужных строках оборотов и только в самом
левом столбце нагрузки -- том, куда зажимается наполнение при закрытом
дросселе. Тяга при разгоне идёт по правым столбцам и не задевается.

Что правится:

    0x10EDA DNSAH, 0x10EDB DNSAL -- дельты порога отсечки относительно
        оборотов возврата, x40 об/мин. Задираем до 0x7F (5080), чтобы
        порог ушёл выше достижимого и отсечка не включалась.
    0x14DC2 KFZWMN -- минимально допустимый угол. В стоке в левых
        столбцах стоит 0.00, то есть позже ВМТ блок не уйдёт. Опускаем
        пол глубже цели, иначе он подрежет.
    0x109F5 KFZW   -- собственно угол.

Обе карты: 16 строк оборотов (ось 0x10103), 12 столбцов нагрузки
(ось 0x10157), знаковый байт x0.75 град.

    python3 tools/popcorn.py "firmware/FBH3ID60 e2 tun csok v2___.bin" \
        -o out/x.bin --angle -15 --rows 11:13 --cols 0:0

После правки контрольную сумму пересчитывает tools/bosch_csum.py.
"""

from __future__ import annotations

import argparse
import sys

KFZW, KFZWMN = 0x109F5, 0x14DC2
COLS = 12
AX_N, AX_L = 0x10103, 0x10157
DNSAH, DNSAL = 0x10EDA, 0x10EDB
STEP = 0.75


def axes(data: bytes) -> tuple[list[int], list[float]]:
    n = [data[AX_N + 1 + i] * 40 for i in range(data[AX_N])]
    l = [data[AX_L + 1 + i] * 0.75 for i in range(data[AX_L])]
    return n, l


def deg(raw: int) -> float:
    return (raw - 256 if raw > 127 else raw) * STEP


def raw(d: float) -> int:
    r = round(d / STEP)
    if not (-128 <= r <= 127):
        raise SystemExit(f"угол {d} вне знакового байта")
    return r & 0xFF


def _range(s: str, limit: int) -> range:
    a, _, b = s.partition(":")
    lo, hi = int(a), int(b) if b else int(a)
    if not (0 <= lo <= hi < limit):
        raise SystemExit(f"диапазон {s} вне 0..{limit - 1}")
    return range(lo, hi + 1)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Поздний угол для отстрелов при сбросе")
    ap.add_argument("firmware")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--angle", type=float, default=-15.0, help="целевой угол, град (по умолчанию -15)")
    ap.add_argument("--floor-margin", type=float, default=9.0,
                    help="насколько глубже цели опустить KFZWMN, град")
    ap.add_argument("--rows", default="11:13", help="строки оборотов, по умолчанию 11:13 = 4000..5000")
    ap.add_argument("--cols", default="0:0", help="столбцы нагрузки, по умолчанию только 0")
    ap.add_argument("--keep-cutoff", action="store_true",
                    help="не трогать отсечку топлива (тогда хлопков не будет)")
    a = ap.parse_args(argv)

    data = bytearray(open(a.firmware, "rb").read())
    n, l = axes(bytes(data))
    rows, cols = _range(a.rows, 16), _range(a.cols, COLS)
    ang, floor = raw(a.angle), raw(a.angle - a.floor_margin)

    if not a.keep_cutoff:
        for addr, name in ((DNSAH, "DNSAH"), (DNSAL, "DNSAL")):
            print(f"{name} 0x{addr:05X}: {data[addr] * 40} -> {0x7F * 40} об/мин")
            data[addr] = 0x7F

    print(f"\nугол {a.angle:+.2f}° (байт 0x{ang:02X}), пол KFZWMN "
          f"{a.angle - a.floor_margin:+.2f}° (байт 0x{floor:02X})")
    for r in rows:
        for c in cols:
            zw, mn = KFZW + r * COLS + c, KFZWMN + r * COLS + c
            print(f"  {n[r]:5.0f} об/мин, {l[c]:5.2f} %:  "
                  f"KFZW 0x{zw:05X} {deg(data[zw]):+7.2f} -> {a.angle:+7.2f}   "
                  f"KFZWMN 0x{mn:05X} {deg(data[mn]):+7.2f} -> {a.angle - a.floor_margin:+7.2f}")
            data[zw], data[mn] = ang, floor

    open(a.out, "wb").write(bytes(data))
    print(f"\nзаписано {a.out}; пересчитайте сумму: "
          f"python3 tools/bosch_csum.py fix {a.out} -o <новый>")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
