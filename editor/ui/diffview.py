#!/usr/bin/env python3
"""
Сравнение прошивок: список изменённых карт.

Байтовый и смысловой слои уже готовы (`core/compare.py` поверх
`tools/fwdiff.py`), здесь только показ. Список даёт то, ради чего
сравнение и делают: какие карты тронуты, сколько ячеек из скольких и на
сколько -- в физических единицах, а не в байтах.

Отдельной строкой показывается, сколько изменённых байт НЕ ЛЕГЛО НИ НА
ОДНУ КАРТУ. Это важнее, чем кажется: у нас размечено чуть больше трети
калибровочных ячеек, и если чужая правка ушла в неразмеченное место,
честнее сказать «вижу изменение, но не знаю чьё», чем промолчать и
оставить человека в уверенности, что всё найдено.

Байты таблицы контрольных сумм считаются отдельно и в «непонятное» не
попадают: они меняются при любой правке и к делу не относятся.
"""

from __future__ import annotations

import os
import sys

from PySide6 import QtCore, QtGui, QtWidgets

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import compare as C                                         # noqa: E402

MODES = [("значения этой", "this"),
         ("значения второй", "other"),
         ("разница", "delta")]


class DiffPanel(QtWidgets.QWidget):
    """Список изменённых карт и переключатель показа."""

    picked = QtCore.Signal(str)          # выбрана карта
    mode_changed = QtCore.Signal(str)    # this | other | delta
    cleared = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.project = None
        self.other = b""
        self.other_path = ""
        self.result = None

        self.pick = QtWidgets.QPushButton("Выбрать вторую прошивку...")
        self.pick.clicked.connect(self.ask_file)
        self.drop = QtWidgets.QPushButton("Закрыть сравнение")
        self.drop.clicked.connect(self.clear)
        self.drop.setEnabled(False)

        self.mode = QtWidgets.QComboBox()
        for title, key in MODES:
            self.mode.addItem(title, key)
        self.mode.setEnabled(False)
        self.mode.currentIndexChanged.connect(
            lambda _i: self.mode_changed.emit(self.mode.currentData()))

        self.head = QtWidgets.QLabel("Вторая прошивка не выбрана")
        self.head.setWordWrap(True)
        self.head.setStyleSheet("color:#444;")

        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["Карта", "Ячеек", "Дельта", "Ед."])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setColumnWidth(0, 150)
        self.table.setColumnWidth(1, 60)
        self.table.setColumnWidth(2, 120)
        self.table.itemSelectionChanged.connect(self._on_pick)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.pick, 1)
        row.addWidget(self.drop)
        lay.addLayout(row)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("в таблице:"))
        row.addWidget(self.mode, 1)
        lay.addLayout(row)
        lay.addWidget(self.head)
        lay.addWidget(self.table, 1)

    # -- работа ----------------------------------------------------------

    def set_project(self, project) -> None:
        self.project = project
        self.clear()

    def ask_file(self) -> None:
        if self.project is None:
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Вторая прошивка",
            os.path.dirname(self.project.bin_path),
            "Образы (*.bin *.ori *.mod);;Все (*)")
        if path:
            self.compare_with(path)

    def compare_with(self, path: str) -> None:
        data = open(path, "rb").read()
        if len(data) != len(self.project.buf):
            QtWidgets.QMessageBox.warning(
                self, "Разный размер",
                "Образы разной длины: %d и %d байт. Сравнивать нечего."
                % (len(self.project.buf), len(data)))
            return
        self.other, self.other_path = data, path
        self.result = C.compare(self.project.layouts,
                                bytes(self.project.buf), data)
        self._fill()
        self.mode.setEnabled(True)
        self.drop.setEnabled(True)
        self.mode_changed.emit(self.mode.currentData())

    def clear(self) -> None:
        self.other, self.other_path, self.result = b"", "", None
        self.table.setRowCount(0)
        self.head.setText("Вторая прошивка не выбрана")
        self.mode.setEnabled(False)
        self.drop.setEnabled(False)
        self.cleared.emit()

    def _fill(self) -> None:
        r = self.result
        self.table.setRowCount(len(r.maps))
        for i, d in enumerate(r.maps):
            cells = [d.name,
                     "%d/%d" % (d.changed, d.total),
                     "%+.3g .. %+.3g" % (d.dmin, d.dmax)
                     if d.dmin != d.dmax else "%+.3g" % d.dmin,
                     d.unit]
            for j, txt in enumerate(cells):
                it = QtWidgets.QTableWidgetItem(txt)
                if j:
                    it.setTextAlignment(
                        QtCore.Qt.AlignmentFlag.AlignRight
                        | QtCore.Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(i, j, it)
        tail = ""
        if r.unmapped_bytes:
            tail = ("<br><b>%d байт изменилось вне известных карт</b> -- "
                    "размечено не всё, эти правки я назвать не могу"
                    % r.unmapped_bytes)
        self.head.setText(
            "<b>%s</b><br>изменённых карт: %d, байт всего: %d "
            "(из них в таблице сумм: %d)%s"
            % (os.path.basename(self.other_path), len(r.maps),
               r.changed_bytes, r.checksum_bytes, tail))

    def _on_pick(self) -> None:
        rows = {i.row() for i in self.table.selectedIndexes()}
        if not rows:
            return
        it = self.table.item(min(rows), 0)
        if it:
            self.picked.emit(it.text())

    def changed_cells(self, name: str) -> set:
        """Какие ячейки этой карты отличаются -- для подсветки в таблице."""
        if not self.result:
            return set()
        for d in self.result.maps:
            if d.name == name:
                return {(r, c) for r, c, _a, _b in d.cells}
        return set()

    def changed_names(self) -> set:
        return {d.name for d in self.result.maps} if self.result else set()
