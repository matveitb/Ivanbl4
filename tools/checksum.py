#!/usr/bin/env python3
"""
checksum -- поиск контрольных сумм в прошивке.

Метод: строятся префиксные суммы, перебираются выровненные пары
(начало, конец) региона, для каждой считается сумма (u8/u16/u32,
с переносом и без) и ищется совпадающее значение, записанное где-то
в самом образе. Совпадение = найденная пара "регион -> ячейка суммы".

Это надёжно находит классические bosch-совместимые суммы вида
"сумма байт/слов региона хранится в u16/u32". Для нестандартных CRC
метод не сработает -- тогда ориентируйтесь на fwdiff: при сравнении
двух рабочих прошивок ячейки сумм видны как мелкие изолированные правки.

Использование:
    python3 tools/checksum.py firmware/stock.bin
    python3 tools/checksum.py firmware/stock.bin --align 0x1000 --width 32
    python3 tools/checksum.py firmware/stock.bin --verify 0x1F000 0x18000 0x1EFFF
"""

from __future__ import annotations

import argparse
import struct
import sys
from collections import defaultdict

import fwlib


def prefix_sums_u8(data: bytes) -> list[int]:
    ps = [0] * (len(data) + 1)
    acc = 0
    for i, b in enumerate(data):
        acc += b
        ps[i + 1] = acc
    return ps


def prefix_sums_u16(data: bytes) -> list[int]:
    n = len(data) // 2
    ps = [0] * (n + 1)
    acc = 0
    words = struct.unpack("<%dH" % n, data[:n * 2])
    for i, w in enumerate(words):
        acc += w
        ps[i + 1] = acc
    return ps


def find_checksums(fw: fwlib.Firmware, align: int, widths: tuple[int, ...],
                   modes: tuple[str, ...], min_span: int, max_results: int):
    data = fw.data
    n = len(data)
    ps8 = prefix_sums_u8(data)
    ps16 = prefix_sums_u16(data)

    # sum value -> список (start, end, mode)
    table: dict[int, list[tuple[int, int, str]]] = defaultdict(list)
    bounds = list(range(0, n + 1, align))

    for i, s in enumerate(bounds):
        for e in bounds[i + 1:]:
            if e - s < min_span:
                continue
            if "u8" in modes:
                total = ps8[e] - ps8[s]
                for w in widths:
                    table[total & ((1 << w) - 1)].append((s, e, "сумма байт u%d" % w))
            if "u16" in modes and s % 2 == 0 and e % 2 == 0:
                total = ps16[e // 2] - ps16[s // 2]
                for w in widths:
                    table[total & ((1 << w) - 1)].append((s, e, "сумма слов u%d" % w))

    hits = []
    # ищем записанные значения
    for off in range(0, n - 3):
        v32 = struct.unpack_from("<I", data, off)[0]
        cand = table.get(v32)
        if cand:
            for s, e, mode in cand:
                if s <= off < e:
                    mode += " (ячейка ВНУТРИ региона)"
                hits.append((off, 32, v32, s, e, mode))
                if len(hits) >= max_results:
                    return hits
    for off in range(0, n - 1, 1):
        v16 = data[off] | (data[off + 1] << 8)
        cand = table.get(v16)
        if cand:
            for s, e, mode in cand:
                if not mode.endswith("u16"):
                    continue
                if s <= off < e:
                    mode += " (ячейка ВНУТРИ региона)"
                hits.append((off, 16, v16, s, e, mode))
                if len(hits) >= max_results:
                    return hits
    return hits


def verify(fw: fwlib.Firmware, cell: int, start: int, end: int) -> None:
    data = fw.data
    print("Проверка региона %s..%s, ячейка %s" %
          (fwlib.hexa(start), fwlib.hexa(end), fwlib.hexa(cell)))
    s8 = sum(data[start:end])
    nwords = (end - start) // 2
    s16 = sum(struct.unpack_from("<%dH" % nwords, data, start))
    stored16 = fw.u16(cell)
    stored32 = fw.u32(cell) if fw.in_range(cell, 4) else None
    print("  сумма байт : 0x%08X  (u16: 0x%04X)" % (s8 & 0xFFFFFFFF, s8 & 0xFFFF))
    print("  сумма слов : 0x%08X  (u16: 0x%04X)" % (s16 & 0xFFFFFFFF, s16 & 0xFFFF))
    print("  в ячейке   : u16=0x%04X" % stored16 +
          ("  u32=0x%08X" % stored32 if stored32 is not None else ""))
    for label, val in (("сумма байт", s8), ("сумма слов", s16)):
        if stored32 is not None and (val & 0xFFFFFFFF) == stored32:
            print("  СОВПАДЕНИЕ: %s == u32 в ячейке" % label)
        if (val & 0xFFFF) == stored16:
            print("  СОВПАДЕНИЕ: %s == u16 в ячейке" % label)


def _auto_int(s: str) -> int:
    return int(s, 0)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Поиск контрольных сумм")
    ap.add_argument("firmware")
    ap.add_argument("--align", type=_auto_int, default=0x1000,
                    help="выравнивание границ региона (по умолчанию 0x1000)")
    ap.add_argument("--width", type=int, nargs="+", default=[16, 32],
                    choices=[16, 32], help="разрядность суммы")
    ap.add_argument("--mode", nargs="+", default=["u8", "u16"],
                    choices=["u8", "u16"], help="суммировать байты и/или слова")
    ap.add_argument("--min-span", type=_auto_int, default=0x1000)
    ap.add_argument("--max-results", type=int, default=200)
    ap.add_argument("--verify", nargs=3, type=_auto_int,
                    metavar=("CELL", "START", "END"),
                    help="проверить конкретную гипотезу")
    args = ap.parse_args(argv)

    fw = fwlib.load(args.firmware)

    if args.verify:
        verify(fw, *args.verify)
        return 0

    print("Поиск контрольных сумм в %s (%d байт)" % (args.firmware, len(fw)))
    print("Выравнивание 0x%X, разрядность %s, режимы %s\n" %
          (args.align, args.width, args.mode))

    hits = find_checksums(fw, args.align, tuple(args.width), tuple(args.mode),
                          args.min_span, args.max_results)
    if not hits:
        print("Совпадений не найдено. Попробуйте другое выравнивание "
              "(--align 0x100 / 0x400) или ищите суммы через fwdiff "
              "по паре рабочих прошивок.")
        return 0

    print("%-10s %5s %-10s %-10s %-10s  %s" %
          ("ЯЧЕЙКА", "РАЗР", "ЗНАЧЕНИЕ", "РЕГИОН НАЧ", "РЕГИОН КОН", "РЕЖИМ"))
    for off, w, v, s, e, mode in hits[:args.max_results]:
        print("%-10s %5d 0x%08X %-10s %-10s  %s" %
              (fwlib.hexa(off), w, v, fwlib.hexa(s), fwlib.hexa(e), mode))
    print("\nВсего совпадений: %d" % len(hits))
    print("ВНИМАНИЕ: часть совпадений случайна. Достоверные признаки -- "
          "ячейка лежит вне региона, регион крупный и выровнен, "
          "а при сравнении двух рабочих прошивок эта ячейка меняется.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
