#!/usr/bin/env python3
"""
Кривая: значения карты как линии по оси X.

Рисуется на QPainter, без matplotlib и QtCharts. Причина не в
принципиальности, а в весе: matplotlib тянет за собой numpy и под сорок
мегабайт, а обещано было, что редактор ставится одной строкой. Кривая --
это полтораста строк рисования.

Показываются ВСЕ строки карты сразу, а текущая -- жирно и с точками.
Так видно не только выбранный срез, но и семейство: расходятся ли
кривые ровно, нет ли одной выпадающей. Именно на этом ловятся кривые
правки -- ступенька, которой в соседних оборотах нет.

Цвет строки берётся из той же палитры, что заливка ячеек и поверхность:
одна карта -- один язык цвета.
"""

from __future__ import annotations

import math
import os
import sys

from PySide6 import QtCore, QtGui, QtWidgets

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mapaccess as M                                       # noqa: E402
import palette                                              # noqa: E402

BG = QtGui.QColor(252, 252, 250)
GRID = QtGui.QColor(222, 222, 218)
AXIS = QtGui.QColor(120, 120, 118)
TEXT = QtGui.QColor(60, 60, 58)


def nice_ticks(lo: float, hi: float, want: int = 6) -> list:
    """Круглые отметки на оси: шаг 1, 2 или 5 на нужном порядке."""
    if hi <= lo:
        return [lo]
    raw = (hi - lo) / max(1, want)
    mag = 10.0 ** int(math.floor(math.log10(raw)))
    for m in (1, 2, 5, 10):
        step = m * mag
        if raw <= step:
            break
    first = step * int(lo / step) + (step if lo > 0 else 0)
    out, v = [], first - step
    while v <= hi + step * 0.5:
        if lo - 1e-9 <= v <= hi + 1e-9:
            out.append(v)
        v += step
    return out or [lo, hi]


class Curve2D(QtWidgets.QWidget):
    """Семейство кривых одной карты."""

    L, R, T, B = 58, 14, 12, 34          # поля под подписи

    def __init__(self, parent=None):
        super().__init__(parent)
        self.project = None
        self.name = ""
        self.lay = None
        self.row = 0
        self.other = b""            # вторая прошивка при сравнении
        self.setMinimumHeight(140)
        self.setMouseTracking(True)
        self._hover = None
        self.setAutoFillBackground(True)

    def set_map(self, project, name: str) -> None:
        self.project, self.name = project, name
        self.lay = project.layout(name)
        self.row = min(self.row, max(0, self.lay.ny - 1))
        self.update()

    def set_row(self, row: int) -> None:
        if self.lay is not None and 0 <= row < self.lay.ny and row != self.row:
            self.row = row
            self.update()

    def set_compare(self, other: bytes) -> None:
        self.other = other or b""
        self.update()

    def refresh(self) -> None:
        self.update()

    # -- рисование -------------------------------------------------------

    def _frame(self) -> QtCore.QRect:
        return QtCore.QRect(self.L, self.T,
                            max(1, self.width() - self.L - self.R),
                            max(1, self.height() - self.T - self.B))

    def paintEvent(self, ev) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        p.fillRect(self.rect(), BG)
        if self.lay is None:
            p.setPen(TEXT)
            p.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter,
                       "Карта не выбрана")
            return

        L, buf = self.lay, self.project.buf
        vals = M.read_phys(buf, L)
        xs, ys = M.axes(buf, L)
        xs = list(xs[:L.nx]) or [0.0]
        if len(set(xs)) < len(xs):
            xs = [float(i) for i in range(L.nx)]
        both = vals + ([M.read_phys(self.other, L)[self.row]]
                       if self.other else [])
        vlo, vhi = palette.span(both)
        pad = (vhi - vlo) * 0.06 or 1.0
        vlo, vhi = vlo - pad, vhi + pad
        xlo, xhi = min(xs), max(xs)
        if xhi <= xlo:
            xhi = xlo + 1.0

        fr = self._frame()

        def px(x):
            return fr.left() + (x - xlo) / (xhi - xlo) * fr.width()

        def py(v):
            return fr.bottom() - (v - vlo) / (vhi - vlo) * fr.height()

        # сетка и подписи
        f = p.font()
        f.setPointSize(8)
        p.setFont(f)
        for v in nice_ticks(vlo, vhi):
            y = py(v)
            p.setPen(GRID)
            p.drawLine(fr.left(), y, fr.right(), y)
            p.setPen(TEXT)
            p.drawText(QtCore.QRectF(0, y - 8, self.L - 6, 16),
                       QtCore.Qt.AlignmentFlag.AlignRight
                       | QtCore.Qt.AlignmentFlag.AlignVCenter, "%g" % v)
        for x in nice_ticks(xlo, xhi):
            xp = px(x)
            p.setPen(GRID)
            p.drawLine(xp, fr.top(), xp, fr.bottom())
            p.setPen(TEXT)
            p.drawText(QtCore.QRectF(xp - 40, fr.bottom() + 3, 80, 14),
                       QtCore.Qt.AlignmentFlag.AlignCenter, "%g" % x)
        p.setPen(AXIS)
        p.drawRect(fr)

        # кривые: сперва все бледно, потом текущая
        ny = L.ny
        for r in range(ny):
            if r == self.row:
                continue
            t = r / max(1, ny - 1)
            c = palette.heat(t)
            c.setAlpha(90)
            p.setPen(QtGui.QPen(c, 1.2))
            self._poly(p, xs, vals[r], px, py)

        # при сравнении та же строка второй прошивки идёт пунктиром:
        # две кривые рядом показывают правку нагляднее любой таблицы
        if self.other:
            try:
                other_row = M.read_phys(self.other, L)[self.row]
                p.setPen(QtGui.QPen(QtGui.QColor(70, 70, 70), 1.6,
                                    QtCore.Qt.PenStyle.DashLine))
                self._poly(p, xs, other_row, px, py)
            except Exception:                               # noqa: BLE001
                pass

        c = palette.heat(self.row / max(1, ny - 1))
        p.setPen(QtGui.QPen(c.darker(120), 2.4))
        self._poly(p, xs, vals[self.row], px, py)
        p.setBrush(QtGui.QBrush(c))
        for i, x in enumerate(xs):
            p.drawEllipse(QtCore.QPointF(px(x), py(vals[self.row][i])), 3, 3)

        # подпись текущей строки и единицы
        label = self.name
        if ny > 1 and self.row < len(ys):
            label += "   строка %g" % ys[self.row]
        if L.unit:
            label += "   [%s]" % L.unit
        if self.other:
            label += "   (пунктир -- вторая прошивка)"
        p.setPen(TEXT)
        f.setBold(True)
        p.setFont(f)
        p.drawText(QtCore.QRectF(fr.left() + 6, fr.top() + 2,
                                 fr.width() - 12, 16),
                   QtCore.Qt.AlignmentFlag.AlignLeft, label)

        # значение под курсором
        if self._hover is not None:
            i = self._hover
            if i < len(xs):
                p.setPen(QtGui.QPen(QtGui.QColor(40, 40, 40), 1,
                                    QtCore.Qt.PenStyle.DashLine))
                p.drawLine(px(xs[i]), fr.top(), px(xs[i]), fr.bottom())
                txt = "%g -> %.3f" % (xs[i], vals[self.row][i])
                p.setPen(TEXT)
                p.drawText(QtCore.QRectF(fr.right() - 160, fr.top() + 2, 154, 16),
                           QtCore.Qt.AlignmentFlag.AlignRight, txt)

    def _poly(self, p, xs, ys, px, py) -> None:
        pts = [QtCore.QPointF(px(xs[i]), py(ys[i]))
               for i in range(min(len(xs), len(ys)))]
        if len(pts) > 1:
            p.drawPolyline(QtGui.QPolygonF(pts))

    # -- мышь ------------------------------------------------------------

    def mouseMoveEvent(self, ev) -> None:
        if self.lay is None:
            return
        fr = self._frame()
        buf = self.project.buf
        xs, _ = M.axes(buf, self.lay)
        xs = list(xs[:self.lay.nx])
        if not xs or fr.width() <= 0:
            return
        xlo, xhi = min(xs), max(xs)
        if xhi <= xlo:
            xhi = xlo + 1.0
        x = xlo + (ev.position().x() - fr.left()) / fr.width() * (xhi - xlo)
        self._hover = min(range(len(xs)), key=lambda i: abs(xs[i] - x))
        self.update()

    def leaveEvent(self, ev) -> None:
        self._hover = None
        self.update()
