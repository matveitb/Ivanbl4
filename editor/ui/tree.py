#!/usr/bin/env python3
"""
Дерево карт слева: группы, поиск, отметка изменённых.

Поиск не фильтрует дерево, а ПЕРЕКЛЮЧАЕТ его в плоский список найденного.
Причина простая: при фильтрации дерева половина групп схлопывается в
пустые заголовки, и глазом искать приходится всё равно. Плоский список из
шести строк честнее, чем дерево из двадцати четырёх узлов, где занято
три.
"""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

ROLE_MAP = QtCore.Qt.ItemDataRole.UserRole + 1


class MapTree(QtWidgets.QWidget):

    selected = QtCore.Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.project = None
        self._changed: set = set()

        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("поиск по имени и описанию")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._rebuild)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["Карта", "Размер"])
        self.tree.setColumnWidth(0, 230)
        self.tree.itemSelectionChanged.connect(self._on_select)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lay.addWidget(self.search)
        lay.addWidget(self.tree)

    # -- наполнение ------------------------------------------------------

    def set_project(self, project) -> None:
        self.project = project
        self._changed = set()
        self.search.clear()
        self._rebuild()

    def mark_changed(self, names) -> None:
        """Пометить изменённые карты жирным, не перестраивая дерево."""
        self._changed = set(names)
        it = QtWidgets.QTreeWidgetItemIterator(self.tree)
        while it.value():
            node = it.value()
            name = node.data(0, ROLE_MAP)
            if name:
                f = node.font(0)
                f.setBold(name in self._changed)
                node.setFont(0, f)
            it += 1

    def _leaf(self, name: str) -> QtWidgets.QTreeWidgetItem:
        L = self.project.layouts[name]
        size = "%dx%d" % (L.nx, L.ny) if L.cells > 1 else "-"
        node = QtWidgets.QTreeWidgetItem([name, size])
        node.setData(0, ROLE_MAP, name)
        node.setToolTip(0, "%s\nданные 0x%05X, %s%d, множитель %g %s\n%s"
                        % (name, L.data_off, "s" if L.signed else "u",
                           L.width * 8, L.factor, L.unit,
                           self.project.desc(name)))
        if not L.editable:
            node.setForeground(0, QtGui.QBrush(QtGui.QColor(140, 140, 140)))
        if name in self._changed:
            f = node.font(0)
            f.setBold(True)
            node.setFont(0, f)
        return node

    def _rebuild(self) -> None:
        self.tree.clear()
        if self.project is None:
            return
        text = self.search.text().strip()
        if text:
            found = self.project.search(text)
            top = QtWidgets.QTreeWidgetItem(
                ["Найдено: %s" % text, str(len(found))])
            for name in found[:2000]:
                top.addChild(self._leaf(name))
            self.tree.addTopLevelItem(top)
            top.setExpanded(True)
            return

        for node in self.project.tree().children:
            top = QtWidgets.QTreeWidgetItem([node.title, str(node.count)])
            for name in node.maps:
                top.addChild(self._leaf(name))
            self.tree.addTopLevelItem(top)

    def _on_select(self) -> None:
        items = self.tree.selectedItems()
        if not items:
            return
        name = items[0].data(0, ROLE_MAP)
        if name:
            self.selected.emit(name)

    def select_map(self, name: str) -> None:
        it = QtWidgets.QTreeWidgetItemIterator(self.tree)
        while it.value():
            node = it.value()
            if node.data(0, ROLE_MAP) == name:
                self.tree.setCurrentItem(node)
                self.tree.scrollToItem(node)
                return
            it += 1
