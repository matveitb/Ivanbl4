#!/usr/bin/env python3
"""
revlimit -- отсечка по оборотам в прошивках FBH3ID60 / Bosch M7.9.7.

Группа лежит подряд по 0x14BB2, все слова u16 LE:

    0x14BB2  DNMAXH    x0.25  гистерезис жёсткого ограничения, во всех
                              прошивках 120 об/мин
    0x14BB4  NMAX      x0.25  собственно отсечка
    0x14BB6  NMAXDV    x0.25  ограничение при неисправном сигнале скорости
    0x14BB8  NMXDKPU   x0.25  предел при неизвестном положении дросселя (1200)
    0x14BBA  TNMAXDV   x0.01  задержка ограничения по NMAXDV (3 с)

Порядок и смысл взяты из DAMOS px5ns03d (блок 0x14B5C..0x14B64, сдвиг +0x56)
и подтверждаются значениями: гистерезис, лимп-хоум 1200 и задержка 3 с
читаются осмысленно во всех восьми прошивках.

    python3 tools/revlimit.py show firmware/FBH3ID60_stok.bin
    python3 tools/revlimit.py set  firmware/FBH3ID60_stok.bin -o out/x.bin --nmax 7000

После правки контрольную сумму пересчитывает tools/bosch_csum.py.
"""

from __future__ import annotations

import argparse
import sys

FIELDS = [
    (0x14BB2, "DNMAXH",  0.25, "об/мин", "гистерезис жёсткого ограничения"),
    (0x14BB4, "NMAX",    0.25, "об/мин", "отсечка"),
    (0x14BB6, "NMAXDV",  0.25, "об/мин", "предел при неисправном сигнале скорости"),
    (0x14BB8, "NMXDKPU", 0.25, "об/мин", "предел при неизвестном положении дросселя"),
    (0x14BBA, "TNMAXDV", 0.01, "с",      "задержка ограничения по NMAXDV"),
]


def get(data: bytes, addr: int) -> int:
    return data[addr] | (data[addr + 1] << 8)


def put(data: bytearray, addr: int, raw: int) -> None:
    data[addr] = raw & 0xFF
    data[addr + 1] = (raw >> 8) & 0xFF


def show(data: bytes) -> None:
    for addr, name, factor, unit, desc in FIELDS:
        print(f"  0x{addr:05X} {name:8s} {get(data, addr) * factor:>8.2f} {unit:6s} -- {desc}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Отсечка по оборотам, Bosch M7.9.7")
    ap.add_argument("action", choices=["show", "set"])
    ap.add_argument("firmware")
    ap.add_argument("-o", "--out", help="куда записать результат (для set)")
    ap.add_argument("--nmax", type=float, help="новая отсечка, об/мин")
    ap.add_argument("--nmaxdv", type=float,
                    help="предел при неисправном сигнале скорости, об/мин "
                         "(обычно трогать не нужно)")
    a = ap.parse_args(argv)

    data = bytearray(open(a.firmware, "rb").read())
    if a.action == "show":
        show(bytes(data))
        return 0

    if not a.out or (a.nmax is None and a.nmaxdv is None):
        raise SystemExit("для set нужны -o и хотя бы один из --nmax / --nmaxdv")

    for value, addr, name in ((a.nmax, 0x14BB4, "NMAX"),
                              (a.nmaxdv, 0x14BB6, "NMAXDV")):
        if value is None:
            continue
        raw = round(value / 0.25)
        if not (0 <= raw <= 0xFFFF):
            raise SystemExit(f"{name} вне диапазона слова")
        old = get(data, addr) * 0.25
        put(data, addr, raw)
        print(f"{name}: {old:.2f} -> {raw * 0.25:.2f} об/мин "
              f"(0x{addr:05X}, слово {raw} = 0x{raw:04X})")

    nmax, nmaxdv = get(data, 0x14BB4) * 0.25, get(data, 0x14BB6) * 0.25
    if nmaxdv > nmax:
        print(f"  внимание: NMAXDV ({nmaxdv:.0f}) выше NMAX ({nmax:.0f}); "
              f"во всех известных прошивках он ниже")

    open(a.out, "wb").write(bytes(data))
    print(f"записано {a.out}; пересчитайте сумму: "
          f"python3 tools/bosch_csum.py fix {a.out} -o <новый>")
    show(bytes(data))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
