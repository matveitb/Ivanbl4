#!/usr/bin/env python3
"""
Поверхность: карта как рельеф.

Рисуется алгоритмом художника -- четырёхугольники сетки сортируются по
глубине и заливаются от дальнего к ближнему. Это тридцать строк
математики вместо целого модуля трёхмерной графики, и на карте 16 на 16
работает мгновенно.

Почему не QtDataVisualization. Сначала я думал, что дело в лицензии, и
записал это в план с оговоркой «проверить, а не поверить мне на слово».
Проверил: в PySide6 6.11 привязка QtDataVisualization помечена теми же
условиями, что и остальной пакет (LGPL-3.0 в числе вариантов), так что
довод не годится, и я его снимаю. Остались другие, и они настоящие:
палитра у поверхности та же, что у заливки ячеек и у кривой, вращение
нужно ровно одно, а лишний модуль на десяток мегабайт ради одного
виджета не окупается.

Рельеф врёт, если не сказать о масштабе: высота всегда растянута на всю
доступную высоту вида, поэтому карта с размахом в один градус выглядит
так же рельефно, как карта с размахом в сорок. Поэтому размах подписан
прямо на виде.
"""

from __future__ import annotations

import math
import os
import sys

from PySide6 import QtCore, QtGui, QtWidgets

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mapaccess as M                                       # noqa: E402
import palette                                              # noqa: E402

BG = QtGui.QColor(250, 250, 248)
TEXT = QtGui.QColor(60, 60, 58)


class Surface3D(QtWidgets.QWidget):
    """Поверхность карты с вращением мышью."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.project = None
        self.name = ""
        self.lay = None
        self.yaw = math.radians(35.0)
        self.pitch = math.radians(45.0)
        self.zoom = 1.0
        self.height_k = 1.2
        self._drag = None
        self.setMinimumHeight(140)
        self.setAutoFillBackground(True)
        self.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)

    def set_map(self, project, name: str) -> None:
        self.project, self.name = project, name
        self.lay = project.layout(name)
        self.update()

    def refresh(self) -> None:
        self.update()

    # -- проекция --------------------------------------------------------

    def _project(self, x: float, y: float, z: float) -> tuple:
        """Модельные координаты -> экранные плюс глубина."""
        ca, sa = math.cos(self.yaw), math.sin(self.yaw)
        X = x * ca - y * sa
        Y = x * sa + y * ca
        ce, se = math.cos(self.pitch), math.sin(self.pitch)
        sx = X
        sy = Y * se - z * ce
        depth = Y * ce + z * se
        return sx, sy, depth

    def paintEvent(self, ev) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        p.fillRect(self.rect(), BG)
        if self.lay is None or self.lay.cells < 4:
            p.setPen(TEXT)
            p.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter,
                       "Поверхность строится для карт от двух строк")
            return

        L, buf = self.lay, self.project.buf
        vals = M.read_phys(buf, L)
        xs, ys = M.axes(buf, L)
        lo, hi = palette.span(vals)

        nx, ny = L.nx, L.ny
        # модельные координаты: сетка в квадрате [-1..1], высота в [0..1]
        def mx(c):
            return -1.0 + 2.0 * c / max(1, nx - 1)

        def my(r):
            return -1.0 + 2.0 * r / max(1, ny - 1)

        def mz(v):
            return (v - lo) / (hi - lo) * self.height_k

        pts = [[self._project(mx(c), my(r), mz(vals[r][c]))
                for c in range(nx)] for r in range(ny)]

        # Масштаб -- компромисс между двумя крайностями, и обе плохи.
        #
        # По самим данным: у гладкой карты крайние сочетания (дальний угол
        # и высокая точка разом) не встречаются, проекция вырождается в
        # узкую полосу, и пологий скат размазывается на всю ширину.
        #
        # По углам коробки, в которой данные лежат: рамка честная и не
        # прыгает при повороте, но пологая карта занимает середину, а
        # сверху и снизу пустота.
        #
        # Берём подгон по данным, но не позволяем ему увеличить больше чем
        # вдвое против коробки. Пологая карта заполняет вид, а вырождения
        # не случается.
        mgn = 26
        w = max(1.0, self.width() - 2 * mgn)
        h = max(1.0, self.height() - 2 * mgn - 16)

        def fit(qs):
            ax = max(q[0] for q in qs) - min(q[0] for q in qs)
            ay = max(q[1] for q in qs) - min(q[1] for q in qs)
            return min(w / max(1e-6, ax), h / max(1e-6, ay))

        box = [self._project(x, y, z)
               for x in (-1.0, 1.0) for y in (-1.0, 1.0)
               for z in (0.0, self.height_k)]
        flat = [q for row in pts for q in row]
        minx = min(q[0] for q in flat)
        maxx = max(q[0] for q in flat)
        miny = min(q[1] for q in flat)
        maxy = max(q[1] for q in flat)
        k = min(fit(flat), 2.0 * fit(box)) * self.zoom
        cx = self.width() / 2.0 - (minx + maxx) / 2.0 * k
        cy = (self.height() - 16) / 2.0 - (miny + maxy) / 2.0 * k

        def scr(q):
            return QtCore.QPointF(cx + q[0] * k, cy + q[1] * k)

        # четырёхугольники от дальнего к ближнему
        quads = []
        for r in range(ny - 1):
            for c in range(nx - 1):
                corners = (pts[r][c], pts[r][c + 1],
                           pts[r + 1][c + 1], pts[r + 1][c])
                depth = sum(q[2] for q in corners) / 4.0
                mid = (vals[r][c] + vals[r][c + 1]
                       + vals[r + 1][c + 1] + vals[r + 1][c]) / 4.0
                quads.append((depth, corners, mid))
        quads.sort(key=lambda q: -q[0])

        pen = QtGui.QPen(QtGui.QColor(255, 255, 255, 140), 0.8)
        for _d, corners, mid in quads:
            col = palette.heat((mid - lo) / (hi - lo))
            p.setBrush(QtGui.QBrush(col))
            p.setPen(pen)
            p.drawPolygon(QtGui.QPolygonF([scr(q) for q in corners]))

        # подписи углов осей и размах
        f = p.font()
        f.setPointSize(8)
        p.setFont(f)
        p.setPen(TEXT)

        def corner_label(r, c, txt):
            q = scr(pts[r][c])
            p.drawText(QtCore.QRectF(q.x() - 45, q.y() - 8, 90, 16),
                       QtCore.Qt.AlignmentFlag.AlignCenter, txt)

        if xs and ys:
            corner_label(0, 0, "%g / %g" % (xs[0], ys[0]))
            corner_label(0, nx - 1, "%g" % xs[min(nx - 1, len(xs) - 1)])
            corner_label(ny - 1, 0, "%g" % ys[min(ny - 1, len(ys) - 1)])

        f.setBold(True)
        p.setFont(f)
        p.drawText(QtCore.QRectF(6, self.height() - 17, self.width() - 12, 15),
                   QtCore.Qt.AlignmentFlag.AlignLeft,
                   "%s   %g .. %g %s   (высота растянута на весь вид)"
                   % (self.name, round(lo, 3), round(hi, 3), L.unit or ""))

    # -- мышь ------------------------------------------------------------

    def mousePressEvent(self, ev) -> None:
        self._drag = ev.position()
        self.setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, ev) -> None:
        if self._drag is None:
            return
        d = ev.position() - self._drag
        self._drag = ev.position()
        self.yaw += d.x() * 0.012
        self.pitch = max(math.radians(-5.0),
                         min(math.radians(88.0), self.pitch + d.y() * 0.008))
        self.update()

    def mouseReleaseEvent(self, ev) -> None:
        self._drag = None
        self.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)

    def wheelEvent(self, ev) -> None:
        self.zoom = max(0.4, min(3.0,
                                 self.zoom * (1.1 if ev.angleDelta().y() > 0
                                              else 1 / 1.1)))
        self.update()

    def mouseDoubleClickEvent(self, ev) -> None:
        """Двойной щелчок -- вернуть исходный поворот."""
        self.yaw, self.pitch, self.zoom = math.radians(35.0), \
            math.radians(45.0), 1.0
        self.update()
