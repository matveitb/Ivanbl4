#!/usr/bin/env python3
"""
bosch_csum -- проверка и пересчёт контрольных сумм M7.9.7 (FBH3ID60).

Формат, установленный на этих прошивках:

  Таблица записей по 16 байт, начинается с 0x1FC00:
      +0  u32  начальный адрес региона (адресное пространство C167)
      +4  u32  конечный адрес региона (включительно)
      +8  u32  контрольная сумма
      +12 u32  побитовое дополнение суммы (~сумма)

  Адрес -> смещение в файле:  offset = addr - 0x800000
  Алгоритм: сумма 16-битных little-endian слов по региону, 32-битный аккумулятор.

Проверено: 32 из 34 записей сходятся точно на всех трёх прошивках.
Две записи (адреса 0x00000000-0x00007FFF) ссылаются на память вне этого
дампа, во всех файлах одинаковы и не пересчитываются.

Использование:
    python3 tools/bosch_csum.py check firmware/stock.bin
    python3 tools/bosch_csum.py fix  правленая.bin -o готовая.bin
"""

from __future__ import annotations

import argparse
import struct
import sys

import fwlib

TABLE_ADDR = 0x1FC00
TABLE_END = 0x20000
RECORD = 16
FLASH_BASE = 0x800000


def sum16(data: bytes, start: int, end_incl: int) -> int:
    """Сумма 16-битных LE слов в регионе [start..end_incl]."""
    n = (end_incl - start + 1) // 2
    return sum(struct.unpack_from("<%dH" % n, data, start)) & 0xFFFFFFFF


def to_offset(addr: int) -> int:
    return addr - FLASH_BASE if addr >= FLASH_BASE else addr


def records(fw: fwlib.Firmware, table: int = TABLE_ADDR):
    """Перебрать записи таблицы. Отдаёт (смещение записи, start, end, sum, notsum)."""
    for off in range(table, min(TABLE_END, len(fw)), RECORD):
        s, e, c, n = struct.unpack_from("<IIII", fw.data, off)
        if s == 0xFFFFFFFF or (s == 0 and e == 0 and c == 0):
            break
        yield off, s, e, c, n


def check(fw: fwlib.Firmware, table: int, verbose: bool = True):
    """Вернуть (список_ошибок, список_пропущенных)."""
    bad, skipped, good = [], [], []
    for off, s, e, c, n in records(fw, table):
        if (c ^ 0xFFFFFFFF) != n:
            skipped.append((off, s, e, "сумма и дополнение не согласованы"))
            continue
        if s < FLASH_BASE:
            # регионы 0x00000000-0x00007FFF описывают память вне этого дампа
            # (внутреннее ПЗУ/ОЗУ). Во всех прошивках одинаковы, не трогаем.
            skipped.append((off, s, e, "не флеш-регион, вне дампа"))
            continue
        fs, fe = to_offset(s), to_offset(e)
        if fe >= len(fw) or fs >= len(fw) or fs > fe:
            skipped.append((off, s, e, "регион вне образа"))
            continue
        calc = sum16(fw.data, fs, fe)
        if calc == c:
            good.append((off, s, e))
        else:
            bad.append((off, s, e, fs, fe, calc, c))
    if verbose:
        print("%-9s %-11s %-11s %-9s %-9s %s" %
              ("ЗАПИСЬ", "НАЧАЛО", "КОНЕЦ", "ФАЙЛ.НАЧ", "ФАЙЛ.КОН", "СТАТУС"))
        for off, s, e in good:
            print("0x%05X   0x%08X 0x%08X 0x%05X   0x%05X   OK" %
                  (off, s, e, to_offset(s), to_offset(e)))
        for off, s, e, fs, fe, calc, c in bad:
            print("0x%05X   0x%08X 0x%08X 0x%05X   0x%05X   НЕВЕРНО "
                  "(записано 0x%08X, надо 0x%08X)" % (off, s, e, fs, fe, c, calc))
        for off, s, e, why in skipped:
            print("0x%05X   0x%08X 0x%08X %-9s %-9s ПРОПУЩЕНА (%s)" %
                  (off, s, e, "-", "-", why))
        print("\nВсего: OK %d, неверных %d, пропущено %d"
              % (len(good), len(bad), len(skipped)))
    return bad, skipped, good


def fix(fw: fwlib.Firmware, table: int) -> tuple[bytearray, int]:
    buf = bytearray(fw.data)
    fixed = 0
    for off, s, e, c, n in records(fw, table):
        if (c ^ 0xFFFFFFFF) != n:
            continue
        if s < FLASH_BASE:
            continue  # см. комментарий в check()
        fs, fe = to_offset(s), to_offset(e)
        if fe >= len(fw) or fs >= len(fw) or fs > fe:
            continue
        # сумму считаем по УЖЕ обновляемому буферу: регионы не пересекаются
        # с таблицей сумм, но на всякий случай берём актуальные байты
        calc = sum16(bytes(buf), fs, fe)
        if calc != c:
            struct.pack_into("<II", buf, off + 8, calc, calc ^ 0xFFFFFFFF)
            fixed += 1
    return buf, fixed


def _auto_int(s: str) -> int:
    return int(s, 0)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Контрольные суммы Bosch M7.9.7")
    ap.add_argument("action", choices=["check", "fix"])
    ap.add_argument("firmware")
    ap.add_argument("-o", "--out", help="куда записать исправленный образ (для fix)")
    ap.add_argument("--table", type=_auto_int, default=TABLE_ADDR,
                    help="адрес таблицы сумм (по умолчанию 0x1FC00)")
    args = ap.parse_args(argv)

    fw = fwlib.load(args.firmware)

    if args.action == "check":
        bad, skipped, good = check(fw, args.table)
        return 1 if bad else 0

    if not args.out:
        print("Для fix укажите -o <файл>", file=sys.stderr)
        return 2
    if args.out == args.firmware:
        print("Отказ: выходной файл совпадает с входным. "
              "Всегда сохраняйте оригинал.", file=sys.stderr)
        return 2

    buf, fixed = fix(fw, args.table)
    with open(args.out, "wb") as fh:
        fh.write(bytes(buf))
    print("Пересчитано записей: %d" % fixed)
    print("Записано: %s" % args.out)

    print("\nПроверка результата:")
    bad, skipped, good = check(fwlib.load(args.out), args.table, verbose=False)
    print("  OK %d, неверных %d, пропущено %d" % (len(good), len(bad), len(skipped)))
    if bad:
        print("  ВНИМАНИЕ: остались неверные суммы", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
