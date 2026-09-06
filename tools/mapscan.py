#!/usr/bin/env python3
"""
mapscan -- эвристический поиск калибровочных карт в прошивке Bosch M7.9.7 (C167).

Ищет структуры вида (классический "Kennfeld" Bosch):

    3D-карта:  nx:u8  ny:u8  X[nx]  Y[ny]  D[nx*ny]
    2D-кривая: n:u8   X[n]   D[n]

Разрядность осей и данных подбирается независимо (u8 / u16-LE), порядок
счётчиков (nx,ny) / (ny,nx) проверяется в обоих вариантах.

Кандидат принимается, если:
  * счётчики в разумных пределах (по умолчанию 2..32);
  * ось строго монотонно возрастает;
  * блок целиком помещается в образ;
  * таблица данных "гладкая" (вторая разность мала относительно размаха).

Использование:
    python3 tools/mapscan.py firmware/stock.bin --out out/maps_stock.json
    python3 tools/mapscan.py firmware/stock.bin --range 0x18000 0x20000 --min-score 0.6
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, asdict, field

import fwlib
from fwlib import Firmware


# --------------------------------------------------------------------------

@dataclass
class MapCandidate:
    addr: int                 # адрес заголовка (первого байта счётчика)
    kind: str                 # "3d" | "2d"
    nx: int
    ny: int
    axis_width: int           # 1 | 2
    data_width: int           # 1 | 2
    order: str                # "nx_ny" | "ny_nx"
    data_addr: int            # адрес начала блока данных
    end: int                  # адрес первого байта за структурой
    x_axis: list[int] = field(default_factory=list)
    y_axis: list[int] = field(default_factory=list)
    score: float = 0.0
    smooth: float = 0.0
    dmin: int = 0
    dmax: int = 0
    refs: int = 0             # сколько раз младшие 16 бит адреса встречены в образе

    @property
    def cells(self) -> int:
        return self.nx * self.ny

    def size(self) -> int:
        return self.end - self.addr


# --------------------------------------------------------------------------
# Проверка осей
# --------------------------------------------------------------------------

def _axis_ok(vals: list[int], width: int) -> bool:
    """Ось должна строго возрастать и иметь осмысленный размах."""
    if len(vals) < 2:
        return False
    if not fwlib.is_monotonic_increasing(vals, strict=True):
        return False
    span = vals[-1] - vals[0]
    if span <= 0:
        return False
    # отбрасываем вырожденные "оси" вида 0,1,2,3... в коде: шаг всегда 1
    # и при этом ось короткая -- слишком часто ложное срабатывание
    steps = [b - a for a, b in zip(vals, vals[1:])]
    if len(set(steps)) == 1 and steps[0] == 1 and len(vals) < 8:
        return False
    # ось из одинаковых мелких шагов при u16 обычно реальна, оставляем
    ceiling = 0xFF if width == 1 else 0xFFFF
    if vals[-1] > ceiling:
        return False
    return True


def _data_ok(flat: list[int], nx: int, ny: int, min_smooth: float) -> tuple[bool, float]:
    matrix = fwlib.reshape(flat, ny, nx)
    s = fwlib.smoothness(matrix)
    return (s >= min_smooth), s


# --------------------------------------------------------------------------
# Сканер
# --------------------------------------------------------------------------

class Scanner:
    def __init__(
        self,
        fw: Firmware,
        min_dim: int = 2,
        max_dim: int = 32,
        min_smooth: float = 0.55,
        min_cells: int = 6,
        axis_widths: tuple[int, ...] = (1, 2),
        data_widths: tuple[int, ...] = (1, 2),
    ) -> None:
        self.fw = fw
        self.min_dim = min_dim
        self.max_dim = max_dim
        self.min_smooth = min_smooth
        self.min_cells = min_cells
        self.axis_widths = axis_widths
        self.data_widths = data_widths

    # -- 3D -------------------------------------------------------------

    def try_3d(self, addr: int) -> list[MapCandidate]:
        fw = self.fw
        out: list[MapCandidate] = []
        if not fw.in_range(addr, 2):
            return out
        a, b = fw.u8(addr), fw.u8(addr + 1)
        if not (self.min_dim <= a <= self.max_dim and self.min_dim <= b <= self.max_dim):
            return out

        for order, (nx, ny) in (("nx_ny", (a, b)), ("ny_nx", (b, a))):
            if nx * ny < self.min_cells:
                continue
            for aw in self.axis_widths:
                xo = addr + 2
                yo = xo + nx * aw
                if not fw.in_range(yo, ny * aw):
                    continue
                try:
                    xs = fw.vec(xo, nx, aw)
                    ys = fw.vec(yo, ny, aw)
                except IndexError:
                    continue
                if not _axis_ok(xs, aw) or not _axis_ok(ys, aw):
                    continue
                do = yo + ny * aw
                for dw in self.data_widths:
                    if not fw.in_range(do, nx * ny * dw):
                        continue
                    try:
                        flat = fw.vec(do, nx * ny, dw)
                    except IndexError:
                        continue
                    ok, s = _data_ok(flat, nx, ny, self.min_smooth)
                    if not ok:
                        continue
                    cand = MapCandidate(
                        addr=addr, kind="3d", nx=nx, ny=ny,
                        axis_width=aw, data_width=dw, order=order,
                        data_addr=do, end=do + nx * ny * dw,
                        x_axis=xs, y_axis=ys, smooth=s,
                        dmin=min(flat), dmax=max(flat),
                    )
                    cand.score = self._score(cand)
                    out.append(cand)
        return out

    # -- 2D -------------------------------------------------------------

    def try_2d(self, addr: int) -> list[MapCandidate]:
        fw = self.fw
        out: list[MapCandidate] = []
        if not fw.in_range(addr, 1):
            return out
        n = fw.u8(addr)
        if not (max(self.min_dim, 3) <= n <= self.max_dim):
            return out
        for aw in self.axis_widths:
            xo = addr + 1
            if not fw.in_range(xo, n * aw):
                continue
            try:
                xs = fw.vec(xo, n, aw)
            except IndexError:
                continue
            if not _axis_ok(xs, aw):
                continue
            do = xo + n * aw
            for dw in self.data_widths:
                if not fw.in_range(do, n * dw):
                    continue
                try:
                    flat = fw.vec(do, n, dw)
                except IndexError:
                    continue
                ok, s = _data_ok(flat, n, 1, self.min_smooth)
                if not ok:
                    continue
                cand = MapCandidate(
                    addr=addr, kind="2d", nx=n, ny=1,
                    axis_width=aw, data_width=dw, order="n",
                    data_addr=do, end=do + n * dw,
                    x_axis=xs, y_axis=[], smooth=s,
                    dmin=min(flat), dmax=max(flat),
                )
                cand.score = self._score(cand)
                out.append(cand)
        return out

    # -- ранжирование ----------------------------------------------------

    def _score(self, c: MapCandidate) -> float:
        """
        Итоговая уверенность 0..1.

        Складывается из гладкости, размера (большие карты -- реже случайность),
        размаха данных и "красоты" размерностей (степени двойки + 1 типичны
        для Bosch: 8, 16, 17...).
        """
        s = c.smooth * 0.55

        cells = c.cells
        s += min(0.20, cells / 256.0 * 0.20)

        span = c.dmax - c.dmin
        ceiling = 0xFF if c.data_width == 1 else 0xFFFF
        if span > 0:
            s += min(0.10, (span / float(ceiling)) * 0.30)

        nice = {4, 5, 6, 8, 9, 10, 12, 16, 17, 20, 24, 32}
        if c.nx in nice:
            s += 0.075
        if c.kind == "3d" and c.ny in nice:
            s += 0.075
        return min(1.0, s)

    # -- полный проход ---------------------------------------------------

    def scan(self, start: int, end: int, kinds=("3d", "2d")) -> list[MapCandidate]:
        cands: list[MapCandidate] = []
        for addr in range(start, min(end, len(self.fw))):
            if "3d" in kinds:
                cands.extend(self.try_3d(addr))
            if "2d" in kinds:
                cands.extend(self.try_2d(addr))
        return cands


# --------------------------------------------------------------------------
# Устранение перекрытий: из пересекающихся кандидатов оставляем лучший
# --------------------------------------------------------------------------

def dedupe(cands: list[MapCandidate]) -> list[MapCandidate]:
    ranked = sorted(cands, key=lambda c: (-c.score, -c.size(), c.addr))
    taken: list[MapCandidate] = []
    occupied: list[tuple[int, int]] = []
    for c in ranked:
        lo, hi = c.addr, c.end
        if any(lo < o_hi and o_lo < hi for o_lo, o_hi in occupied):
            continue
        taken.append(c)
        occupied.append((lo, hi))
    return sorted(taken, key=lambda c: c.addr)


def annotate_refs(fw: Firmware, cands: list[MapCandidate]) -> None:
    for c in cands:
        # ссылаются обычно на заголовок карты
        c.refs = len(fwlib.find_le16_refs(fw, c.addr, limit=32))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _auto_int(s: str) -> int:
    return int(s, 0)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Поиск калибровочных карт Bosch M7.9.7")
    ap.add_argument("firmware")
    ap.add_argument("--range", nargs=2, type=_auto_int, metavar=("START", "END"),
                    help="ограничить диапазон поиска (по умолчанию весь файл)")
    ap.add_argument("--min-smooth", type=float, default=0.55,
                    help="минимальная гладкость таблицы (0..1), по умолчанию 0.55")
    ap.add_argument("--min-score", type=float, default=0.0,
                    help="отбросить кандидатов с итоговой оценкой ниже")
    ap.add_argument("--min-dim", type=int, default=2)
    ap.add_argument("--max-dim", type=int, default=32)
    ap.add_argument("--min-cells", type=int, default=6)
    ap.add_argument("--kinds", default="3d,2d", help="что искать: 3d,2d")
    ap.add_argument("--no-dedupe", action="store_true")
    ap.add_argument("--no-refs", action="store_true", help="не считать xref (быстрее)")
    ap.add_argument("--out", help="записать результат в JSON")
    ap.add_argument("--top", type=int, default=40, help="сколько показать в консоли")
    args = ap.parse_args(argv)

    fw = fwlib.load(args.firmware)
    start, end = (args.range if args.range else (0, len(fw)))
    kinds = tuple(k.strip() for k in args.kinds.split(",") if k.strip())

    sc = Scanner(fw, min_dim=args.min_dim, max_dim=args.max_dim,
                 min_smooth=args.min_smooth, min_cells=args.min_cells)

    print("Образ: %s (%d байт)" % (args.firmware, len(fw)), file=sys.stderr)
    print("Диапазон: %s..%s, типы: %s" % (fwlib.hexa(start), fwlib.hexa(end), ",".join(kinds)),
          file=sys.stderr)

    cands = sc.scan(start, end, kinds=kinds)
    print("Сырых кандидатов: %d" % len(cands), file=sys.stderr)

    if not args.no_dedupe:
        cands = dedupe(cands)
        print("После снятия перекрытий: %d" % len(cands), file=sys.stderr)

    if args.min_score > 0:
        cands = [c for c in cands if c.score >= args.min_score]
        print("После фильтра по оценке: %d" % len(cands), file=sys.stderr)

    if not args.no_refs:
        annotate_refs(fw, cands)

    # консольный отчёт
    shown = sorted(cands, key=lambda c: -c.score)[:args.top]
    print()
    print("%-10s %-4s %7s %5s %5s %6s %6s %5s  %s" %
          ("АДРЕС", "ТИП", "РАЗМЕР", "ОСЬX", "ОСЬY", "ОЦЕН", "ГЛАДК", "XREF", "ДАННЫЕ"))
    for c in shown:
        dims = "%dx%d" % (c.nx, c.ny) if c.kind == "3d" else "%d" % c.nx
        print("%-10s %-4s %7s %5s %5s %6.2f %6.2f %5d  %s..%s @%s" % (
            fwlib.hexa(c.addr), c.kind, dims,
            "u%d" % (c.axis_width * 8), "u%d" % (c.data_width * 8),
            c.score, c.smooth, c.refs,
            c.dmin, c.dmax, fwlib.hexa(c.data_addr)))

    if args.out:
        payload = {
            "firmware": args.firmware,
            "size": len(fw),
            "range": [start, end],
            "params": {
                "min_smooth": args.min_smooth, "min_score": args.min_score,
                "min_dim": args.min_dim, "max_dim": args.max_dim,
                "min_cells": args.min_cells,
            },
            "maps": [asdict(c) for c in sorted(cands, key=lambda c: c.addr)],
        }
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1, ensure_ascii=False)
        print("\nЗаписано: %s (%d карт)" % (args.out, len(cands)), file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
