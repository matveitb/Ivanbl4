#!/usr/bin/env python3
"""
fwdiff -- побайтовое сравнение двух прошивок с группировкой изменений.

Ключевой приём для вашей задачи: две прошивки "csok" отличаются только
углами зажигания. Значит изменённые области = ТЕЛА карт зажигания.
Оси при этом не менялись, поэтому:

  * изменённая область даёт точный адрес и размер блока данных;
  * структура карты (nx, ny, оси) достраивается сканированием НАЗАД
    от начала области -- там лежит заголовок nx/ny и оси.

Мелкие изолированные области (2-4 байта) вдали от карт -- почти наверняка
контрольные суммы, они помечаются отдельно.

Использование:
    python3 tools/fwdiff.py firmware/csok1.bin firmware/csok2.bin
    python3 tools/fwdiff.py a.bin b.bin --identify --out out/diff.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, asdict, field

import fwlib
from fwlib import Firmware


@dataclass
class Region:
    start: int
    end: int                     # эксклюзивно
    changed_bytes: int = 0
    kind: str = "data"           # "data" | "checksum?" | "single"
    note: str = ""
    # результат попытки распознать структуру карты
    map_addr: int | None = None
    map_kind: str | None = None
    nx: int | None = None
    ny: int | None = None
    axis_width: int | None = None
    data_width: int | None = None
    x_axis: list[int] = field(default_factory=list)
    y_axis: list[int] = field(default_factory=list)
    delta_min: int = 0
    delta_max: int = 0

    @property
    def size(self) -> int:
        return self.end - self.start


# --------------------------------------------------------------------------

def diff_regions(a: bytes, b: bytes, merge_gap: int = 16) -> list[Region]:
    """
    Найти изменённые байты и склеить их в области, объединяя те, что
    разделены менее чем merge_gap неизменёнными байтами.
    """
    n = min(len(a), len(b))
    changed: list[int] = [i for i in range(n) if a[i] != b[i]]
    if not changed:
        return []

    regions: list[Region] = []
    start = prev = changed[0]
    count = 1
    for i in changed[1:]:
        if i - prev - 1 <= merge_gap:
            prev = i
            count += 1
        else:
            regions.append(Region(start=start, end=prev + 1, changed_bytes=count))
            start = prev = i
            count = 1
    regions.append(Region(start=start, end=prev + 1, changed_bytes=count))

    if len(a) != len(b):
        regions.append(Region(start=n, end=max(len(a), len(b)),
                              changed_bytes=abs(len(a) - len(b)),
                              kind="size", note="файлы разного размера"))
    return regions


# Таблица контрольных сумм M7.9.7 (см. tools/bosch_csum.py)
CSUM_TABLE = (0x1FC00, 0x20000)


def classify(regions: list[Region]) -> None:
    """
    Помечать правку как контрольную сумму можно ТОЛЬКО если она попала в саму
    таблицу сумм. Раньше сюда попадала любая мелкая изолированная правка, и
    из-за этого настоящая находка -- отсечка по оборотам (2 байта по 0x14BB4) --
    была подписана как "вероятно контрольная сумма".
    """
    lo, hi = CSUM_TABLE
    for r in regions:
        if r.start < hi and lo < r.end:
            r.kind = "checksum"
            r.note = "правка внутри таблицы контрольных сумм 0x1FC00"
        elif r.size <= 4 and r.changed_bytes <= 4:
            r.kind = "small"
            r.note = "мелкая изолированная правка (скаляр или порог)"
        elif r.size == 1:
            r.kind = "single"


# --------------------------------------------------------------------------
# Достраивание структуры карты по изменённому блоку данных
# --------------------------------------------------------------------------

def identify_region(fw: Firmware, r: Region, max_back: int = 160,
                    min_dim: int = 2, max_dim: int = 32) -> bool:
    """
    Область r -- предположительно тело карты. Ищем заголовок карты в
    max_back байтах перед началом области так, чтобы блок данных карты
    накрывал изменённые байты.

    Возвращает True, если структура распознана.
    """
    import mapscan

    sc = mapscan.Scanner(fw, min_dim=min_dim, max_dim=max_dim,
                         min_smooth=0.0,      # тело мы уже "знаем", гладкость не фильтруем
                         min_cells=4)

    best = None
    lo = max(0, r.start - max_back)
    for addr in range(lo, r.start + 1):
        for c in sc.try_3d(addr) + sc.try_2d(addr):
            # блок данных должен накрывать изменённую область
            if c.data_addr <= r.start and c.end >= r.end:
                # предпочитаем ту, чей блок данных начинается точнее на границе
                key = (abs(c.data_addr - r.start), -c.cells, -c.smooth)
                if best is None or key < best[0]:
                    best = (key, c)
    if best is None:
        return False

    c = best[1]
    r.map_addr = c.addr
    r.map_kind = c.kind
    r.nx, r.ny = c.nx, c.ny
    r.axis_width, r.data_width = c.axis_width, c.data_width
    r.x_axis, r.y_axis = c.x_axis, c.y_axis
    return True


def delta_stats(a: Firmware, b: Firmware, r: Region) -> None:
    """Разброс изменений (в единицах data_width, со знаком)."""
    w = r.data_width or 1
    deltas = []
    off = r.start
    while off + w <= r.end:
        if w == 1:
            deltas.append(b.u8(off) - a.u8(off))
        else:
            deltas.append(b.u16(off) - a.u16(off))
        off += w
    if deltas:
        r.delta_min, r.delta_max = min(deltas), max(deltas)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _auto_int(s: str) -> int:
    return int(s, 0)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Сравнение двух прошивок с группировкой изменений")
    ap.add_argument("file_a")
    ap.add_argument("file_b")
    ap.add_argument("--merge-gap", type=int, default=16,
                    help="склеивать области, разделённые менее чем N неизменёнными байтами")
    ap.add_argument("--identify", action="store_true",
                    help="пытаться достроить структуру карты для каждой области")
    ap.add_argument("--max-back", type=int, default=160,
                    help="сколько байт перед областью просматривать в поисках заголовка")
    ap.add_argument("--dump", action="store_true", help="печатать таблицы до/после")
    ap.add_argument("--out", help="записать результат в JSON")
    args = ap.parse_args(argv)

    a = fwlib.load(args.file_a)
    b = fwlib.load(args.file_b)

    print("A: %s (%d байт)" % (args.file_a, len(a)))
    print("B: %s (%d байт)" % (args.file_b, len(b)))
    if len(a) != len(b):
        print("ВНИМАНИЕ: размеры файлов различаются", file=sys.stderr)

    regions = diff_regions(a.data, b.data, merge_gap=args.merge_gap)
    classify(regions)

    total_changed = sum(r.changed_bytes for r in regions)
    print("Изменено байт: %d, областей: %d\n" % (total_changed, len(regions)))

    if args.identify:
        for r in regions:
            if r.kind in ("checksum?", "size"):
                continue
            identify_region(a, r, max_back=args.max_back)

    for r in regions:
        delta_stats(a, b, r)

    print("%-10s %-10s %7s %8s  %s" % ("НАЧАЛО", "КОНЕЦ", "РАЗМЕР", "ИЗМ.БАЙТ", "РАСПОЗНАНО"))
    for r in regions:
        if r.map_addr is not None:
            dims = "%dx%d" % (r.nx, r.ny) if r.map_kind == "3d" else "%d" % r.nx
            desc = "карта @%s %s %s осьu%d данныеu%d Δ[%+d..%+d]" % (
                fwlib.hexa(r.map_addr), r.map_kind, dims,
                r.axis_width * 8, r.data_width * 8, r.delta_min, r.delta_max)
        elif r.kind == "checksum?":
            desc = "контрольная сумма? Δ[%+d..%+d]" % (r.delta_min, r.delta_max)
        else:
            desc = "структура не распознана Δ[%+d..%+d]" % (r.delta_min, r.delta_max)
        print("%-10s %-10s %7d %8d  %s" % (
            fwlib.hexa(r.start), fwlib.hexa(r.end), r.size, r.changed_bytes, desc))

    if args.dump:
        for r in regions:
            if r.map_addr is None or r.map_kind != "3d":
                continue
            print("\n=== карта @ %s (%dx%d) ===" % (fwlib.hexa(r.map_addr), r.nx, r.ny))
            w = r.data_width
            # блок данных начинается сразу после осей
            do = r.map_addr + 2 + (r.nx + r.ny) * r.axis_width
            fa = a.vec(do, r.nx * r.ny, w)
            fb = b.vec(do, r.nx * r.ny, w)
            print("ось X:", r.x_axis)
            print("ось Y:", r.y_axis)
            print("-- A --")
            print(fwlib.fmt_matrix(fwlib.reshape(fa, r.ny, r.nx)))
            print("-- B --")
            print(fwlib.fmt_matrix(fwlib.reshape(fb, r.ny, r.nx)))
            print("-- B - A --")
            d = [x - y for x, y in zip(fb, fa)]
            print(fwlib.fmt_matrix(fwlib.reshape(d, r.ny, r.nx)))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"a": args.file_a, "b": args.file_b,
                       "regions": [asdict(r) for r in regions]},
                      fh, indent=1, ensure_ascii=False)
        print("\nЗаписано: %s" % args.out, file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
