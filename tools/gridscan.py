#!/usr/bin/env python3
"""
gridscan -- поиск ТАБЛИЦ БЕЗ ЗАГОЛОВКА (голых сеток) в прошивке.

Зачем отдельный инструмент. В прошивке FBH3ID60 (M7.9.7) трёхмерные карты
лежат НЕ в классическом формате Bosch с байтами nx/ny и осями внутри блока
(его ищет mapscan.py), а как голые прямоугольные массивы: оси хранятся
отдельно и общие для нескольких карт. Такую карту можно найти только по
внутренней структуре данных.

Метод. Для блока байт перебирается предполагаемая ширина W и считается
разброс разностей на шаге W:

    score(W) = среднее|x[i+W] - x[i]| + СКО|x[i+W] - x[i]|

У настоящей таблицы шириной W соседние строки похожи, поэтому на верной
ширине score резко минимален. На проверке по трём заранее известным картам
верная ширина занимает первое место с запасом (ближайший конкурент -- всегда
кратная ширина 2W, что и ожидается).

Найдя ширину, инструмент подбирает фазу строк и наращивает таблицу вверх
и вниз, пока соседние строки остаются согласованными.

Использование:
    python3 tools/gridscan.py firmware/stock.bin --range 0x10000 0x1C000
    python3 tools/gridscan.py firmware/stock.bin --range 0x10500 0x10600 --dump
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, asdict, field

import fwlib
from fwlib import Firmware


@dataclass
class Grid:
    addr: int
    width: int
    rows: int
    end: int
    score: float          # чем меньше, тем лучше согласованы строки
    dmin: int = 0
    dmax: int = 0
    mono_rows: int = 0    # сколько строк монотонны (убывают или возрастают)
    note: str = ""

    @property
    def cells(self) -> int:
        return self.width * self.rows

    def size(self) -> int:
        return self.end - self.addr


# --------------------------------------------------------------------------

def width_score(data: bytes, start: int, length: int, w: int) -> float:
    end = min(start + length, len(data)) - w
    if end <= start:
        return float("inf")
    n = end - start
    total = 0
    for i in range(start, end):
        total += abs(data[i + w] - data[i])
    mean = total / n
    var = 0.0
    for i in range(start, end):
        d = abs(data[i + w] - data[i]) - mean
        var += d * d
    return mean + (var / n) ** 0.5


def best_width(data: bytes, start: int, length: int,
               wmin: int, wmax: int) -> tuple[int, float, float]:
    """Вернуть (лучшая ширина, её score, score ближайшего некратного конкурента)."""
    scores = []
    for w in range(wmin, wmax + 1):
        scores.append((width_score(data, start, length, w), w))
    scores.sort()
    if not scores:
        return 0, float("inf"), float("inf")
    best_s, best_w = scores[0]
    rival = float("inf")
    for s, w in scores[1:]:
        # кратные ширины не считаем конкурентами -- это гармоники
        if w % best_w == 0 or best_w % w == 0:
            continue
        rival = s
        break
    return best_w, best_s, rival


def row_gap(data: bytes, a: int, b: int, w: int) -> float:
    """Средняя абсолютная разность между двумя строками."""
    return sum(abs(data[a + j] - data[b + j]) for j in range(w)) / float(w)


def row_gap_max(data: bytes, a: int, b: int, w: int) -> int:
    """Максимальная разность по отдельной ячейке между двумя строками."""
    return max(abs(data[a + j] - data[b + j]) for j in range(w))


def row_range(data: bytes, a: int, w: int) -> int:
    row = data[a:a + w]
    return max(row) - min(row) if row else 0


def grow(fw: Firmware, seed: int, w: int, lo_limit: int, hi_limit: int,
         tol_abs: float, tol_rel: float) -> tuple[int, int]:
    """
    Нарастить таблицу вверх и вниз от строки, начинающейся в seed.
    Строка принимается, если она согласована с уже принятой соседней.
    """
    data = fw.data

    def ok(a: int, b: int) -> bool:
        """a -- строка-кандидат, b -- уже принятая строка (якорь)."""
        if a < lo_limit or b < lo_limit:
            return False
        if a + w > hi_limit or b + w > hi_limit:
            return False
        # Допуск считаем ТОЛЬКО по размаху принятой строки. Если брать
        # максимум из двух, строка-мусор с огромным размахом (например,
        # разделитель 0xFF) сама себе разрешает попасть в таблицу.
        rng = row_range(data, b, w)
        tol = max(tol_abs, tol_rel * rng)
        if row_gap(data, a, b, w) > tol:
            return False
        # Отдельная ячейка не должна выпадать далеко за общий допуск --
        # это отсекает строки, где совпало "в среднем", но есть выброс.
        return row_gap_max(data, a, b, w) <= max(tol * 3.0, tol_abs * 3.0)

    top = seed
    while ok(top - w, top):
        top -= w
    bot = seed
    while ok(bot + w, bot):
        bot += w
    return top, bot + w


def scan(fw: Firmware, start: int, end: int, wmin: int, wmax: int,
         probe: int, step: int, max_score: float, min_margin: float,
         min_rows: int, tol_abs: float, tol_rel: float) -> list[Grid]:
    data = fw.data
    found: list[Grid] = []
    covered: list[tuple[int, int]] = []

    pos = start
    while pos + probe <= end:
        if any(lo <= pos < hi for lo, hi in covered):
            pos += step
            continue

        chunk = data[pos:pos + probe]
        if fwlib.is_blank(chunk):
            pos += step
            continue

        w, s, rival = best_width(data, pos, probe, wmin, wmax)
        if w == 0 or s > max_score or rival < s * min_margin:
            pos += step
            continue

        # подобрать фазу строк: пробуем все смещения внутри ширины
        best = None
        for phase in range(w):
            seed = pos + phase
            if seed + w > end:
                continue
            top, bot = grow(fw, seed, w, start, end, tol_abs, tol_rel)
            rows = (bot - top) // w
            if rows < min_rows:
                continue
            gaps = [row_gap(data, top + r * w, top + (r + 1) * w, w)
                    for r in range(rows - 1)]
            avg = sum(gaps) / len(gaps) if gaps else 999.0
            key = (-rows, avg)
            if best is None or key < best[0]:
                best = (key, top, bot, rows, avg)

        if best is None:
            pos += step
            continue

        _, top, bot, rows, avg = best
        flat = list(data[top:bot])
        mono = 0
        for r in range(rows):
            row = flat[r * w:(r + 1) * w]
            if (fwlib.is_monotonic_increasing(row, strict=False)
                    or fwlib.is_monotonic_decreasing(row, strict=False)):
                mono += 1

        g = Grid(addr=top, width=w, rows=rows, end=bot, score=avg,
                 dmin=min(flat), dmax=max(flat), mono_rows=mono)
        found.append(g)
        covered.append((top, bot))
        pos = max(pos + step, bot)

    return sorted(found, key=lambda g: g.addr)


def _auto_int(s: str) -> int:
    return int(s, 0)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Поиск таблиц без заголовка")
    ap.add_argument("firmware")
    ap.add_argument("--range", nargs=2, type=_auto_int, metavar=("START", "END"))
    ap.add_argument("--wmin", type=int, default=4)
    ap.add_argument("--wmax", type=int, default=32)
    ap.add_argument("--probe", type=int, default=96,
                    help="размер окна для определения ширины")
    ap.add_argument("--step", type=int, default=8, help="шаг сканирования")
    ap.add_argument("--max-score", type=float, default=12.0,
                    help="порог согласованности строк (меньше -- строже)")
    ap.add_argument("--min-margin", type=float, default=1.25,
                    help="во сколько раз лучшая ширина должна обойти конкурента")
    ap.add_argument("--min-rows", type=int, default=4)
    ap.add_argument("--tol-abs", type=float, default=6.0,
                    help="абсолютный допуск на разность соседних строк")
    ap.add_argument("--tol-rel", type=float, default=0.45,
                    help="доля размаха строки как допуск")
    ap.add_argument("--min-cells", type=int, default=24)
    ap.add_argument("--dump", action="store_true")
    ap.add_argument("--out")
    args = ap.parse_args(argv)

    fw = fwlib.load(args.firmware)
    start, end = args.range if args.range else (0, len(fw))

    print("Образ: %s, диапазон %s..%s" %
          (args.firmware, fwlib.hexa(start), fwlib.hexa(end)), file=sys.stderr)

    grids = scan(fw, start, end, args.wmin, args.wmax, args.probe, args.step,
                 args.max_score, args.min_margin, args.min_rows,
                 args.tol_abs, args.tol_rel)
    grids = [g for g in grids if g.cells >= args.min_cells]

    print("Найдено таблиц: %d\n" % len(grids), file=sys.stderr)
    print("%-10s %-10s %8s %7s %7s %7s  %s" %
          ("АДРЕС", "КОНЕЦ", "РАЗМЕР", "СОГЛАС", "МОНОСТР", "ЯЧЕЕК", "ЗНАЧЕНИЯ"))
    for g in grids:
        print("%-10s %-10s %8s %7.2f %4d/%-3d %7d  %d..%d" % (
            fwlib.hexa(g.addr), fwlib.hexa(g.end),
            "%dx%d" % (g.width, g.rows), g.score, g.mono_rows, g.rows,
            g.cells, g.dmin, g.dmax))

    if args.dump:
        for g in grids:
            print("\n=== таблица @%s  %dx%d ===" %
                  (fwlib.hexa(g.addr), g.width, g.rows))
            flat = list(fw.data[g.addr:g.end])
            print(fwlib.fmt_matrix(fwlib.reshape(flat, g.rows, g.width), width=4))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"firmware": args.firmware,
                       "range": [start, end],
                       "grids": [asdict(g) for g in grids]},
                      fh, indent=1, ensure_ascii=False)
        print("\nЗаписано: %s" % args.out, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
