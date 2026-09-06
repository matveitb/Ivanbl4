#!/usr/bin/env python3
"""
fwinfo -- обзор образа прошивки: карта энтропии, поиск кода/данных, строки, ID блока.

Помогает быстро понять:
  * где код (высокая энтропия, ~5.5-7 бит/байт), где калибровки (~3-6),
    где пустая флеш (0xFF/0x00);
  * какие текстовые идентификаторы зашиты (номер Bosch, версия ПО, VIN-подобные);
  * границы блоков, с которых стоит начинать поиск карт.

Использование:
    python3 tools/fwinfo.py firmware/stock.bin
    python3 tools/fwinfo.py firmware/stock.bin --block 1024 --strings-min 6
"""

from __future__ import annotations

import argparse
import re
import sys

import fwlib


PRINTABLE = set(range(0x20, 0x7F)) | {0x0A, 0x0D, 0x09}

# Характерные идентификаторы, встречающиеся в прошивках Bosch
ID_PATTERNS = [
    (r"\b0261[0-9A-Z]{6}\b",        "номер блока Bosch (0261...)"),
    (r"\b1037[0-9]{6}\b",           "номер ПО Bosch (1037...)"),
    (r"\bM7\.9\.7\b",               "версия Motronic"),
    (r"\bME7[\.\d]*\b",             "версия Motronic"),
    (r"\bBOSCH\b",                  "производитель"),
    (r"\b[A-Z0-9]{2,4}[- ]?\d{4,6}\b", "возможный номер калибровки"),
]


def blocks(fw, size: int):
    for off in range(0, len(fw), size):
        yield off, fw.data[off:off + size]


def classify_block(chunk: bytes) -> str:
    if fwlib.is_blank(chunk):
        return "ПУСТО"
    e = fwlib.entropy(chunk)
    if e >= 7.2:
        return "сжато/шум"
    if e >= 5.8:
        return "КОД"
    if e >= 3.2:
        return "ДАННЫЕ/КАЛИБРОВКИ"
    return "таблицы/константы"


def entropy_profile(fw, block: int) -> list[tuple[int, float, str]]:
    return [(off, fwlib.entropy(chunk), classify_block(chunk))
            for off, chunk in blocks(fw, block)]


def merge_profile(prof: list[tuple[int, float, str]], block: int):
    """Склеить соседние блоки одного класса в непрерывные регионы."""
    if not prof:
        return []
    out = []
    start, _, cls = prof[0]
    ent_acc, cnt = prof[0][1], 1
    for off, e, c in prof[1:]:
        if c == cls:
            ent_acc += e
            cnt += 1
        else:
            out.append((start, off, cls, ent_acc / cnt))
            start, cls, ent_acc, cnt = off, c, e, 1
    out.append((start, prof[-1][0] + block, cls, ent_acc / cnt))
    return out


def find_strings(fw, minlen: int = 5) -> list[tuple[int, str]]:
    out = []
    cur, start = [], 0
    for i, b in enumerate(fw.data):
        if b in PRINTABLE and b not in (0x0A, 0x0D, 0x09):
            if not cur:
                start = i
            cur.append(chr(b))
        else:
            if len(cur) >= minlen:
                out.append((start, "".join(cur)))
            cur = []
    if len(cur) >= minlen:
        out.append((start, "".join(cur)))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Обзор образа прошивки")
    ap.add_argument("firmware")
    ap.add_argument("--block", type=int, default=1024, help="размер блока для энтропии")
    ap.add_argument("--strings-min", type=int, default=5)
    ap.add_argument("--max-strings", type=int, default=60)
    args = ap.parse_args(argv)

    fw = fwlib.load(args.firmware)

    print("=" * 74)
    print("ФАЙЛ    : %s" % args.firmware)
    print("РАЗМЕР  : %d байт (0x%X, %s)" % (len(fw), len(fw), fwlib.human_size(len(fw))))
    known = {0x8000: "32 КБ", 0x10000: "64 КБ", 0x20000: "128 КБ",
             0x40000: "256 КБ", 0x80000: "512 КБ", 0x100000: "1 МБ"}
    if len(fw) in known:
        print("          соответствует стандартному объёму флеш %s" % known[len(fw)])
    else:
        print("          ВНИМАНИЕ: нестандартный объём -- возможно, это дамп с "
              "заголовком или частичное чтение")
    print("ЭНТРОПИЯ: %.2f бит/байт по всему образу" % fwlib.entropy(fw.data))
    print("=" * 74)

    print("\nКАРТА РЕГИОНОВ (блок %d байт)\n" % args.block)
    prof = entropy_profile(fw, args.block)
    merged = merge_profile(prof, args.block)
    print("%-10s %-10s %9s %7s  %s" % ("НАЧАЛО", "КОНЕЦ", "РАЗМЕР", "ЭНТР", "КЛАСС"))
    for start, end, cls, e in merged:
        if end - start < args.block:
            continue
        print("%-10s %-10s %9d %7.2f  %s" %
              (fwlib.hexa(start), fwlib.hexa(end), end - start, e, cls))

    cal = [(s, e2) for s, e2, c, _ in merged if c in ("ДАННЫЕ/КАЛИБРОВКИ", "таблицы/константы")]
    if cal:
        print("\nРекомендуемые диапазоны для поиска карт (--range):")
        for s, e2 in cal:
            if e2 - s >= args.block * 2:
                print("   --range 0x%X 0x%X   (%d байт)" % (s, e2, e2 - s))

    print("\nСТРОКИ (мин. длина %d)\n" % args.strings_min)
    strs = find_strings(fw, args.strings_min)
    interesting = []
    for off, s in strs:
        for pat, why in ID_PATTERNS:
            if re.search(pat, s):
                interesting.append((off, s, why))
                break
    if interesting:
        print("-- вероятные идентификаторы блока/ПО --")
        seen = set()
        for off, s, why in interesting[:args.max_strings]:
            if s in seen:
                continue
            seen.add(s)
            print("  %s  %-40s  %s" % (fwlib.hexa(off), s[:40], why))
        print()
    print("-- первые %d строк --" % min(args.max_strings, len(strs)))
    for off, s in strs[:args.max_strings]:
        print("  %s  %s" % (fwlib.hexa(off), s[:60]))
    print("\nВсего строк: %d" % len(strs))

    # C167: таблица векторов прерываний в начале образа
    print("\nНАЧАЛО ОБРАЗА (таблица векторов C167, первые 32 байта):")
    print("  " + " ".join("%02X" % b for b in fw.data[:32]))
    if len(fw) >= 4:
        print("  Первое слово (LE): 0x%04X" % fw.u16(0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
