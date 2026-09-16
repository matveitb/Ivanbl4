#!/usr/bin/env python3
"""
Сохранение образа с пересчётом контрольных сумм.

Блок не примет файл с неверными суммами -- это первое, обо что
спотыкаются в самодельных редакторах. Поэтому пересчёт здесь не отдельная
кнопка, которую можно забыть, а часть сохранения, с видимым
переключателем.

Сам пересчёт уже решён в tools/bosch_csum.py и переписывать его незачем.
Здесь только две вещи сверх него.

1. ПОИСК ТАБЛИЦЫ. У нашей Spectra она по 0x1FC00, но редактор должен
   открывать и чужие прошивки, где адрес другой. Ищем по форме записи:
   16 байт start/end/sum/~sum, причём сумма и её дополнение обязаны
   сходиться, а границы -- лежать в образе. Три подряд таких записи
   случайным совпадением не бывают.

2. РЕЗЕРВНАЯ КОПИЯ. Перезапись поверх оригинала без копии -- потеря
   стока, а сток восстановить неоткуда.
"""

from __future__ import annotations

import os
import shutil
import struct
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bosch_csum                                           # noqa: E402
import fwlib                                                # noqa: E402

FLASH_BASE = 0x800000


@dataclass
class SaveResult:
    path: str
    table: int = -1
    fixed: int = 0
    records: int = 0
    backup: str = ""
    note: str = ""


def _plausible(buf: bytes, off: int) -> bool:
    if off + 16 > len(buf):
        return False
    s, e, c, n = struct.unpack_from("<IIII", buf, off)
    if (c ^ 0xFFFFFFFF) != n:
        return False
    if s >= e:
        return False
    # границы записываются адресами в пространстве флеша
    fs, fe = s - FLASH_BASE if s >= FLASH_BASE else s, \
        e - FLASH_BASE if e >= FLASH_BASE else e
    return 0 <= fs < fe < len(buf)


def find_table(buf: bytes, hint: int = bosch_csum.TABLE_ADDR,
               need: int = 3) -> int:
    """
    Адрес таблицы сумм. Сначала проверяем привычное место, потом ищем.

    Возвращает -1, если ничего похожего нет -- тогда сохранять надо без
    пересчёта и честно об этом сказать, а не подставлять наугад.
    """
    def run_at(off: int) -> int:
        k = 0
        while _plausible(buf, off + k * 16):
            k += 1
        return k

    if run_at(hint) >= need:
        return hint
    for off in range(0, len(buf) - 16, 4):
        if _plausible(buf, off) and run_at(off) >= need:
            return off
    return -1


def recalc(buf: bytearray, table: int = -1) -> tuple[bytearray, int, int]:
    """Пересчитать суммы. Возвращает (буфер, адрес таблицы, сколько исправлено)."""
    if table < 0:
        table = find_table(bytes(buf))
    if table < 0:
        return bytearray(buf), -1, 0
    fw = fwlib.Firmware(bytes(buf))
    out, fixed = bosch_csum.fix(fw, table)
    return out, table, fixed


def verify(buf: bytes, table: int) -> tuple[int, int]:
    """Сколько записей неверны и сколько всего. Ничего не печатает."""
    if table < 0:
        return 0, 0
    fw = fwlib.Firmware(bytes(buf))
    bad, skipped, good = bosch_csum.check(fw, table, verbose=False)
    return len(bad), len(bad) + len(good) + len(skipped)


def save(buf: bytearray, path: str, fix_checksums: bool = True,
         table: int = -1, backup: bool = True) -> SaveResult:
    res = SaveResult(path=path)
    data = bytearray(buf)

    if fix_checksums:
        data, res.table, res.fixed = recalc(data, table)
        if res.table < 0:
            res.note = "таблица сумм не найдена -- записано без пересчёта"
        else:
            bad, total = verify(bytes(data), res.table)
            res.records = total
            if bad:
                res.note = "после пересчёта осталось неверных записей: %d" % bad
    else:
        res.note = "пересчёт сумм отключён"

    if backup and os.path.exists(path):
        bak = path + ".bak"
        if not os.path.exists(bak):
            shutil.copy2(path, bak)
            res.backup = bak

    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)
    return res
