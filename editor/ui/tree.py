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
    groups_changed = QtCore.Signal()

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
        self.tree.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.itemSelectionChanged.connect(self._on_select)
        self.tree.setContextMenuPolicy(
            QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)

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
        tip = ("%s\nданные 0x%05X, %s%d, множитель %g %s\n%s"
               % (name, L.data_off, "s" if L.signed else "u",
                  L.width * 8, L.factor, L.unit, self.project.desc(name)))
        over = self.project.overlaps(name)
        if over:
            tip += "\nделит байты с: " + ", ".join(over[:6])
            node.setForeground(0, QtGui.QBrush(QtGui.QColor(155, 28, 28)))
        node.setToolTip(0, tip)
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

    # -- свои группы ------------------------------------------------------

    def picked_maps(self) -> list:
        """Выделенные карты. Щелчок по заголовку берёт всю группу."""
        out = []
        for node in self.tree.selectedItems():
            name = node.data(0, ROLE_MAP)
            if name:
                out.append(name)
            else:
                out.extend(node.child(i).data(0, ROLE_MAP)
                           for i in range(node.childCount()))
        return [n for n in dict.fromkeys(out) if n]

    def _menu(self, pos) -> None:
        if self.project is None:
            return
        names = self.picked_maps()
        if not names:
            return
        m = QtWidgets.QMenu(self)
        what = ("«%s»" % names[0]) if len(names) == 1 else "%d карт" % len(names)
        sub = m.addMenu("Положить %s в свою группу" % what)
        for title in sorted(self.project.user_groups):
            sub.addAction(title, lambda _=False, t=title:
                          self._add(t, names))
        if self.project.user_groups:
            sub.addSeparator()
        sub.addAction("Новая группа...", lambda: self._add_new(names))

        node = self.tree.itemAt(pos)
        group_title = None
        if node is not None and node.data(0, ROLE_MAP) is None:
            group_title = node.text(0)
        elif node is not None and node.parent() is not None:
            group_title = node.parent().text(0)
        if group_title in self.project.user_groups:
            m.addAction("Убрать из «%s»" % group_title,
                        lambda: self._remove(group_title, names))
            m.addSeparator()
            m.addAction("Переименовать группу «%s»..." % group_title,
                        lambda: self._rename(group_title))
            m.addAction("Удалить группу «%s»" % group_title,
                        lambda: self._drop(group_title))
        m.exec(self.tree.viewport().mapToGlobal(pos))

    def _add(self, title: str, names) -> None:
        self.project.add_to_group(title, names)
        self._after_group_change()

    def _add_new(self, names) -> None:
        title, ok = QtWidgets.QInputDialog.getText(
            self, "Новая группа", "Название:")
        if ok and title.strip():
            self._add(title.strip(), names)

    def _remove(self, title: str, names) -> None:
        self.project.remove_from_group(title, names)
        self._after_group_change()

    def _rename(self, title: str) -> None:
        new, ok = QtWidgets.QInputDialog.getText(
            self, "Переименовать группу", "Название:", text=title)
        if ok and new.strip():
            self.project.rename_group(title, new.strip())
            self._after_group_change()

    def _drop(self, title: str) -> None:
        self.project.drop_group(title)
        self._after_group_change()

    def _after_group_change(self) -> None:
        self.project.save_project_file()
        self._rebuild()
        self.groups_changed.emit()

    def select_map(self, name: str) -> None:
        it = QtWidgets.QTreeWidgetItemIterator(self.tree)
        while it.value():
            node = it.value()
            if node.data(0, ROLE_MAP) == name:
                self.tree.setCurrentItem(node)
                self.tree.scrollToItem(node)
                return
            it += 1
