#!/usr/bin/env python3
"""
Правки над выделением, отмена и повтор.

Единица истории -- CellEdit: одна операция над произвольным набором ячеек
одной карты. Внутри лежат СЫРЫЕ значения «было» и «стало», а не
физические. Так сделано намеренно: физическое значение при записи
округляется к сетке и зажимается в диапазон типа, поэтому обратный
пересчёт «стало -> было» через физику не вернул бы исходные байты. Сырые
возвращают образ побайтово -- это проверяется тестом.

Операции над выделением
-----------------------
=   поставить значение
+   прибавить
-   вычесть
*   умножить
%   изменить на долю (×(1 + p/100))
протянуть   линейно между крайними ячейками выделения
сгладить    усреднить с соседями

«Протянуть» считает не по номерам ячеек, а ПО ЗНАЧЕНИЯМ ОСИ. Оси у нас
неравномерные (обороты идут 440, 760, 1000, ... с переменным шагом), и
протяжка по индексу дала бы излом там, где по физике его быть не должно.

Каждая операция возвращает CellEdit и НЕ ТРОГАЕТ буфер. Применяет только
History -- чтобы не было правок мимо истории.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mapaccess as M                                       # noqa: E402
from geometry import MapLayout                              # noqa: E402


@dataclass
class CellEdit:
    """Одна операция: что за карта, какие ячейки и чем были."""
    map_name: str
    title: str = ""
    cells: list = field(default_factory=list)   # (r, c, было, стало) сырые

    def __bool__(self) -> bool:
        return bool(self.cells)

    @property
    def count(self) -> int:
        return len(self.cells)


class History:
    """Стек команд. Применение идёт только отсюда."""

    def __init__(self, limit: int = 500):
        self._done: list = []
        self._undone: list = []
        self.limit = limit
        self._saved_at = 0

    # -- состояние -------------------------------------------------------

    @property
    def dirty(self) -> bool:
        return len(self._done) != self._saved_at

    def mark_saved(self) -> None:
        self._saved_at = len(self._done)

    @property
    def can_undo(self) -> bool:
        return bool(self._done)

    @property
    def can_redo(self) -> bool:
        return bool(self._undone)

    def undo_title(self) -> str:
        return self._done[-1].title if self._done else ""

    def redo_title(self) -> str:
        return self._undone[-1].title if self._undone else ""

    # -- работа ----------------------------------------------------------

    def apply(self, buf: bytearray, lay: MapLayout, edit: CellEdit) -> CellEdit:
        if not edit:
            return edit
        for r, c, _old, new in edit.cells:
            M.set_raw(buf, lay, r, c, new)
        self._done.append(edit)
        self._undone.clear()
        if len(self._done) > self.limit:
            drop = len(self._done) - self.limit
            del self._done[:drop]
            self._saved_at = max(0, self._saved_at - drop)
        return edit

    def undo(self, buf: bytearray, layouts: dict) -> CellEdit | None:
        if not self._done:
            return None
        edit = self._done.pop()
        lay = layouts[edit.map_name]
        for r, c, old, _new in edit.cells:
            M.set_raw(buf, lay, r, c, old)
        self._undone.append(edit)
        return edit

    def redo(self, buf: bytearray, layouts: dict) -> CellEdit | None:
        if not self._undone:
            return None
        edit = self._undone.pop()
        lay = layouts[edit.map_name]
        for r, c, _old, new in edit.cells:
            M.set_raw(buf, lay, r, c, new)
        self._done.append(edit)
        return edit


# --------------------------------------------------------------------------
# Построение операций
# --------------------------------------------------------------------------

def _build(buf, lay: MapLayout, sel, title: str, fn) -> CellEdit:
    """
    Общая часть: пройти выделение, посчитать новое сырое, отбросить
    ячейки, которые не изменились.

    fn(старое_физическое, r, c) -> новое физическое
    """
    ed = CellEdit(lay.name, title)
    for r, c in sel:
        old = M.get_raw(buf, lay, r, c)
        want = fn(M.raw_to_phys(lay, old), r, c)
        new = M.phys_to_raw(lay, want)
        if new != old:
            ed.cells.append((r, c, old, new))
    return ed


def op_set(buf, lay, sel, value: float) -> CellEdit:
    return _build(buf, lay, sel, "= %g" % value, lambda v, r, c: value)


def op_add(buf, lay, sel, delta: float) -> CellEdit:
    return _build(buf, lay, sel, "%+g" % delta, lambda v, r, c: v + delta)


def op_mul(buf, lay, sel, k: float) -> CellEdit:
    return _build(buf, lay, sel, "x %g" % k, lambda v, r, c: v * k)


def op_percent(buf, lay, sel, pct: float) -> CellEdit:
    k = 1.0 + pct / 100.0
    return _build(buf, lay, sel, "%+g %%" % pct, lambda v, r, c: v * k)


def op_raw_add(buf, lay, sel, delta: int) -> CellEdit:
    """Прибавить к СЫРОМУ значению -- когда важен ровно один шаг сетки."""
    ed = CellEdit(lay.name, "сырое %+d" % delta)
    lo, hi = lay.raw_range()
    for r, c in sel:
        old = M.get_raw(buf, lay, r, c)
        new = max(lo, min(hi, old + int(delta)))
        if new != old:
            ed.cells.append((r, c, old, new))
    return ed


def _axis_or_index(vals, n) -> list:
    """Ось для протяжки. Если она вырождена -- берём номера ячеек."""
    if len(vals) >= n and len(set(vals[:n])) == n:
        return list(vals[:n])
    return [float(i) for i in range(n)]


def op_interpolate(buf, lay: MapLayout, sel) -> CellEdit:
    """
    Протянуть: значения между краями выделения кладутся на прямую,
    проведённую по значениям оси.

    Выделение из одной строки тянется по X, из одного столбца -- по Y,
    прямоугольник -- билинейно по четырём углам. Ячейки-края остаются
    как были: они и есть опора.
    """
    from torque import interp

    rows = sorted({r for r, _ in sel})
    cols = sorted({c for _, c in sel})
    if len(rows) < 2 and len(cols) < 2:
        return CellEdit(lay.name, "протянуть")

    xs_all, ys_all = M.axes(buf, lay)
    xs = _axis_or_index(xs_all, lay.nx)
    ys = _axis_or_index(ys_all, lay.ny)
    phys = M.read_phys(buf, lay)
    inside = set(sel)

    if len(rows) == 1 or len(cols) == 1:
        # одномерная протяжка вдоль выделенной линии
        if len(cols) > 1:
            axis, idx, fixed = xs, cols, rows[0]
            get = lambda i: phys[fixed][i]                   # noqa: E731
            at = lambda i: (fixed, i)                        # noqa: E731
        else:
            axis, idx, fixed = ys, rows, cols[0]
            get = lambda i: phys[i][fixed]                   # noqa: E731
            at = lambda i: (i, fixed)                        # noqa: E731
        ax = [axis[i] for i in idx]
        ay = [get(idx[0]), get(idx[-1])]
        want = {at(i): interp([ax[0], ax[-1]], ay, axis[i]) for i in idx}
    else:
        r0, r1 = rows[0], rows[-1]
        c0, c1 = cols[0], cols[-1]
        corners = ((phys[r0][c0], phys[r0][c1]), (phys[r1][c0], phys[r1][c1]))
        span_x = (xs[c1] - xs[c0]) or 1.0
        span_y = (ys[r1] - ys[r0]) or 1.0
        want = {}
        for r in rows:
            ty = (ys[r] - ys[r0]) / span_y
            for c in cols:
                tx = (xs[c] - xs[c0]) / span_x
                top = corners[0][0] + (corners[0][1] - corners[0][0]) * tx
                bot = corners[1][0] + (corners[1][1] - corners[1][0]) * tx
                want[(r, c)] = top + (bot - top) * ty

    sub = [rc for rc in want if rc in inside]
    return _build(buf, lay, sub, "протянуть",
                  lambda v, r, c: want[(r, c)])


def op_smooth(buf, lay: MapLayout, sel) -> CellEdit:
    """
    Сгладить: ячейка заменяется средним с соседями ВНУТРИ ВЫДЕЛЕНИЯ.

    Соседи снаружи не берутся -- иначе операция затягивала бы в выделение
    чужие значения, и человек получал бы правку там, где ничего не
    выделял. Сама ячейка идёт с весом 2, соседи с весом 1: одно нажатие
    сглаживает заметно, но не смывает форму.
    """
    inside = set(sel)
    phys = M.read_phys(buf, lay)
    want = {}
    for r, c in sel:
        acc, wt = phys[r][c] * 2.0, 2.0
        for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            n = (r + dr, c + dc)
            if n in inside:
                acc += phys[n[0]][n[1]]
                wt += 1.0
        want[(r, c)] = acc / wt
    return _build(buf, lay, sel, "сгладить", lambda v, r, c: want[(r, c)])


OPS = {
    "=": op_set, "+": op_add, "*": op_mul, "%": op_percent,
}
