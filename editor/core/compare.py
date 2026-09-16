#!/usr/bin/env python3
"""
Сравнение двух прошивок в терминах карт.

Байтовый слой уже есть и переписывать его незачем: tools/fwdiff.py
находит изменённые области и помечает служебные (таблицу контрольных
сумм). Здесь только надстройка -- наложить области на карты из
загруженного A2L и сказать, что именно изменилось и на сколько.

Почему не fwdiff.identify_region. Он честно пытается опознать карту по
структуре, перебирая до 160 адресов назад через mapscan.Scanner, и это
медленно. Нам опознавать нечего: имена и раскладки уже пришли из A2L.

Поиск карты по адресу -- бинарный по отсортированному списку начал, а не
перебор всех восьмисот на каждую область.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

import fwdiff                                               # noqa: E402
import geometry                                             # noqa: E402
import mapaccess as M                                       # noqa: E402


@dataclass
class MapDiff:
    name: str
    changed: int = 0            # изменённых ячеек
    total: int = 0
    dmin: float = 0.0           # дельта в физических единицах
    dmax: float = 0.0
    unit: str = ""
    cells: list = field(default_factory=list)   # (r, c, было, стало) физ.
    layout: object = None


@dataclass
class DiffResult:
    maps: list = field(default_factory=list)
    changed_bytes: int = 0
    unmapped_bytes: int = 0
    checksum_bytes: int = 0
    regions: list = field(default_factory=list)


class _Index:
    """Поиск карт, накрывающих байтовый диапазон."""

    def __init__(self, layouts: dict):
        self.items = sorted(
            (L for L in layouts.values() if L.size > 0),
            key=lambda L: L.data_off)
        self.starts = [L.data_off for L in self.items]
        self.max_size = max((L.size for L in self.items), default=0)

    def covering(self, lo: int, hi: int) -> list:
        if not self.items:
            return []
        # карта может начинаться намного раньше искомого места
        i = bisect.bisect_right(self.starts, hi) - 1
        out = []
        while i >= 0 and self.items[i].data_off + self.max_size >= lo:
            L = self.items[i]
            if L.data_off < hi and L.end > lo:
                out.append(L)
            i -= 1
        return out


def compare(layouts: dict, a: bytes, b: bytes, merge_gap: int = 16) -> DiffResult:
    regions = fwdiff.diff_regions(a, b, merge_gap=merge_gap)
    fwdiff.classify(regions)
    idx = _Index(layouts)
    res = DiffResult(regions=regions)
    per: dict = {}

    for r in regions:
        if r.kind == "size":
            continue
        res.changed_bytes += r.changed_bytes
        if r.kind == "checksum":
            res.checksum_bytes += r.changed_bytes
            continue
        hit = idx.covering(r.start, r.end)
        if not hit:
            res.unmapped_bytes += r.changed_bytes
            continue
        for L in hit:
            per.setdefault(L.name, L)

    for name, L in per.items():
        d = MapDiff(name=name, total=L.cells, unit=L.unit, layout=L)
        deltas = []
        for row in range(L.ny):
            for col in range(L.nx):
                off = M.cell_offset(L, row, col)
                if a[off:off + L.width] == b[off:off + L.width]:
                    continue
                va = M.raw_to_phys(L, int.from_bytes(
                    a[off:off + L.width], "little", signed=L.signed))
                vb = M.raw_to_phys(L, int.from_bytes(
                    b[off:off + L.width], "little", signed=L.signed))
                d.cells.append((row, col, va, vb))
                deltas.append(vb - va)
        if not d.cells:
            continue
        d.changed = len(d.cells)
        d.dmin, d.dmax = min(deltas), max(deltas)
        res.maps.append(d)

    res.maps.sort(key=lambda d: -d.changed)
    return res


def compare_files(a2l, buf_a: bytes, buf_b: bytes, am=None,
                  merge_gap: int = 16) -> DiffResult:
    if am is None:
        am = geometry.detect_addressing(a2l, len(buf_a))
    layouts = geometry.resolve_all(a2l, buf_a, am)
    return compare(layouts, buf_a, buf_b, merge_gap)
