#!/usr/bin/env python3
"""
Таблица карты: показ, заливка, правка.

Три вещи, которые здесь важнее красоты.

1. ПОКАЗЫВАЕМ ТО, ЧТО ЛЕГЛО. Введённое значение округляется к шагу сетки
   и зажимается в диапазон типа. После правки ячейка перерисовывается из
   БУФЕРА, а не из введённого текста. Иначе человек видит 14.3, а в блок
   уедет 14.25 -- и он об этом не узнает.

2. ПРАВКА ИДЁТ ТОЛЬКО ЧЕРЕЗ ИСТОРИЮ. Таблица не пишет в буфер сама, она
   просит проект применить CellEdit. Тогда любая правка отменяема, в том
   числе набранная с клавиатуры.

3. ИЗМЕНЁННЫЕ ЯЧЕЙКИ ВИДНО. Отличие от исходного образа помечается
   подчёркиванием, а отличие от второй прошивки при сравнении -- рамкой.
   При переносе правок это единственный способ не потерять, что уже
   сделано.

4. В РЕЖИМЕ СРАВНЕНИЯ ВСЕГДА ПОДПИСАНО, ЧЬИ ЧИСЛА ПОКАЗАНЫ. Таблица
   умеет показывать значения этой прошивки, второй или разницу. Правка
   разрешена только в первом случае: править файл, глядя на чужие числа,
   -- верный способ испортить не то.
"""

from __future__ import annotations

import os
import sys

from PySide6 import QtCore, QtGui, QtWidgets

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import edits                                                # noqa: E402
import mapaccess as M                                       # noqa: E402
import palette                                              # noqa: E402

ROLE_DIFF = QtCore.Qt.ItemDataRole.UserRole + 2


class CellPainter(QtWidgets.QStyledItemDelegate):
    """
    Рисование ячейки: заливка по значению плюс ВИДИМОЕ выделение.

    Обычное выделение Qt -- это закраска ячейки системным цветом. Поверх
    нашей заливки оно либо не видно, либо стирает её и вместе с ней всю
    картину карты. А выделение здесь -- главный рабочий жест: выделил
    область, нажал «+». Не видеть его нельзя.

    Поэтому выделение рисуется не заливкой, а рамкой и лёгким осветлением
    поверх цвета значения: и что выделено видно, и форма карты остаётся
    читаемой.
    """

    def paint(self, painter, option, index):
        sel = bool(option.state & QtWidgets.QStyle.StateFlag.State_Selected)
        diff = bool(index.data(ROLE_DIFF))
        opt = QtWidgets.QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        # снимаем штатную закраску выделения -- рисуем своё
        opt.state &= ~QtWidgets.QStyle.StateFlag.State_Selected
        QtWidgets.QApplication.style().drawControl(
            QtWidgets.QStyle.ControlElement.CE_ItemViewItem, opt, painter)
        if diff:
            r = option.rect.adjusted(1, 1, -2, -2)
            painter.save()
            painter.setPen(QtGui.QPen(QtGui.QColor(150, 20, 20), 2))
            painter.drawRect(r)
            painter.restore()
        if sel:
            r = option.rect.adjusted(0, 0, -1, -1)
            painter.save()
            painter.fillRect(r, QtGui.QColor(255, 255, 255, 60))
            pen = QtGui.QPen(QtGui.QColor(20, 20, 20), 2)
            painter.setPen(pen)
            painter.drawRect(r)
            painter.restore()


class MapGrid(QtWidgets.QTableWidget):
    """Сетка значений одной карты."""

    edited = QtCore.Signal(str)          # имя карты -- окну, чтобы обновить всё

    def __init__(self, parent=None):
        super().__init__(parent)
        self.project = None
        self.map_name = ""
        self.lay = None
        self._loading = False
        self.shading = True
        self.digits = 2
        self.other = b""            # вторая прошивка при сравнении
        self.mode = "this"          # this | other | delta
        self.diff_cells: set = set()
        self.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.ContiguousSelection)
        self.setAlternatingRowColors(False)
        self.itemChanged.connect(self._on_item_changed)
        f = QtGui.QFont("monospace")
        f.setStyleHint(QtGui.QFont.StyleHint.TypeWriter)
        self.setFont(f)
        self.horizontalHeader().setDefaultSectionSize(64)
        self.verticalHeader().setDefaultSectionSize(22)
        self.setItemDelegate(CellPainter(self))

    # -- показ -----------------------------------------------------------

    def set_map(self, project, name: str) -> None:
        self.project = project
        self.map_name = name
        self.lay = project.layout(name)
        self.refresh()

    def set_compare(self, other: bytes, mode: str, cells) -> None:
        """Вторая прошивка, что показывать и какие ячейки обвести."""
        self.other = other or b""
        self.mode = mode if self.other else "this"
        self.diff_cells = set(cells or ())
        self.refresh()

    def refresh(self) -> None:
        if self.lay is None:
            return
        L, buf = self.lay, self.project.buf
        mine = M.read_phys(buf, L)
        xs, ys = M.axes(buf, L)
        orig = self.project.original

        theirs = M.read_phys(self.other, L) if self.other else None
        comparing = bool(self.other) and self.mode != "this"
        if comparing:
            vals = theirs if self.mode == "other" else \
                [[theirs[r][c] - mine[r][c] for c in range(L.nx)]
                 for r in range(L.ny)]
        else:
            vals = mine
        lo, hi = palette.span(vals)

        self._loading = True
        self.clear()
        self.setRowCount(L.ny)
        self.setColumnCount(L.nx)
        self.setHorizontalHeaderLabels(
            [self._axis_label(xs, c) for c in range(L.nx)])
        self.setVerticalHeaderLabels(
            [self._axis_label(ys, r) for r in range(L.ny)])

        # править можно только свои числа: правка при показе чужих или
        # разницы -- верный способ записать не то и не туда
        ro = (not L.editable) or comparing
        for r in range(L.ny):
            for c in range(L.nx):
                v = vals[r][c]
                it = QtWidgets.QTableWidgetItem(("%%.%df" % self.digits) % v)
                it.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                if self.shading:
                    col = palette.heat((v - lo) / (hi - lo))
                    it.setBackground(col)
                    it.setForeground(palette.text_on(col))
                off = M.cell_offset(L, r, c)
                if bytes(buf[off:off + L.width]) != orig[off:off + L.width]:
                    f = it.font()
                    f.setBold(True)
                    f.setUnderline(True)
                    it.setFont(f)
                if (r, c) in self.diff_cells:
                    it.setData(ROLE_DIFF, True)
                    it.setToolTip("эта: %g\nвторая: %g\nразница: %+g"
                                  % (mine[r][c],
                                     theirs[r][c] if theirs else 0.0,
                                     (theirs[r][c] - mine[r][c])
                                     if theirs else 0.0))
                if ro:
                    it.setFlags(it.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                self.setItem(r, c, it)
        self._loading = False
        self._fit_columns()

    def _fit_columns(self) -> None:
        """
        Небольшая карта раскладывается на всё окно, большая листается.

        Смысл не в красоте: карта читается формой, а форма видна только
        когда таблица занимает поле целиком. Двенадцать столбцов в углу
        экрана с серой пустотой рядом читаются заметно хуже.
        """
        hh, vh = self.horizontalHeader(), self.verticalHeader()
        if not self.columnCount():
            return
        Mode = QtWidgets.QHeaderView.ResizeMode
        if self.columnCount() * 64 + vh.width() < self.viewport().width():
            hh.setSectionResizeMode(Mode.Stretch)
        else:
            hh.setSectionResizeMode(Mode.Fixed)
            hh.setDefaultSectionSize(64)
        if self.rowCount() * 22 < self.viewport().height():
            vh.setSectionResizeMode(Mode.Stretch)
        else:
            vh.setSectionResizeMode(Mode.Fixed)
            vh.setDefaultSectionSize(22)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._fit_columns()

    def _axis_label(self, vals, i: int) -> str:
        if i >= len(vals):
            return str(i)
        v = vals[i]
        return "%d" % round(v) if abs(v - round(v)) < 0.05 else "%.2f" % v

    # -- выделение -------------------------------------------------------

    def selection(self) -> list:
        """Выделенные ячейки. Если ничего не выделено -- текущая."""
        cells = sorted({(i.row(), i.column()) for i in self.selectedIndexes()})
        if not cells and self.currentItem() is not None:
            cells = [(self.currentRow(), self.currentColumn())]
        return cells

    def select_all_cells(self) -> None:
        self.selectAll()

    # -- правка ----------------------------------------------------------

    def _apply(self, edit) -> None:
        if not edit:
            return
        self.project.apply(edit)
        self.refresh()
        self.edited.emit(self.map_name)

    def run_op(self, kind: str, value: float = 0.0) -> str:
        """Операция над выделением. Возвращает сообщение для строки состояния."""
        if self.lay is None or not self.lay.editable:
            return "карта только для чтения"
        # Запрет на правку в режиме сравнения снимался только с ячеек
        # (ItemIsEditable), а кнопки полосы инструментов шли мимо него и
        # прекрасно правили файл, пока на экране были чужие числа.
        # Поймано проверкой; теперь запрет один на оба пути.
        if self.other and self.mode != "this":
            return ("на экране %s -- правка запрещена, вернитесь к "
                    "значениям этой прошивки"
                    % ("значения второй прошивки" if self.mode == "other"
                       else "разница"))
        sel = self.selection()
        if not sel:
            return "ничего не выделено"
        buf, L = self.project.buf, self.lay
        if kind == "=":
            ed = edits.op_set(buf, L, sel, value)
        elif kind == "+":
            ed = edits.op_add(buf, L, sel, value)
        elif kind == "-":
            ed = edits.op_add(buf, L, sel, -value)
        elif kind == "*":
            ed = edits.op_mul(buf, L, sel, value)
        elif kind == "%":
            ed = edits.op_percent(buf, L, sel, value)
        elif kind == "raw":
            ed = edits.op_raw_add(buf, L, sel, int(value))
        elif kind == "interp":
            ed = edits.op_interpolate(buf, L, sel)
        elif kind == "smooth":
            ed = edits.op_smooth(buf, L, sel)
        else:
            return "неизвестная операция %s" % kind
        n = ed.count
        self._apply(ed)
        if not n:
            return "%s: ничего не изменилось (уже на этом значении или упёрлось в границу типа)" % ed.title
        return "%s: ячеек изменено %d из %d" % (ed.title, n, len(sel))

    def _on_item_changed(self, it: QtWidgets.QTableWidgetItem) -> None:
        if self._loading or self.lay is None:
            return
        if self.other and self.mode != "this":
            self.refresh()
            return
        txt = it.text().replace(",", ".").strip()
        try:
            v = float(txt)
        except ValueError:
            self.refresh()
            return
        ed = edits.op_set(self.project.buf, self.lay,
                          [(it.row(), it.column())], v)
        self._apply(ed)
