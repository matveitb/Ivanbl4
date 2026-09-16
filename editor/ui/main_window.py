#!/usr/bin/env python3
"""
Главное окно: слева дерево, справа таблица, внизу сведения о карте.

Раскладка повторяет ChipTuningPRO намеренно -- не из подражания, а потому
что она правильная для этой работы: список карт всегда на виду, таблица
занимает всё остальное, операции над выделением лежат в одной полосе
сверху и достаются одним движением.

Про контрольные суммы. Переключатель стоит прямо в полосе инструментов и
включён по умолчанию. Отдельной кнопки "пересчитать суммы" нет
сознательно: её забывают нажать, и блок не принимает файл. Пересчёт --
часть сохранения.
"""

from __future__ import annotations

import os
import sys

from PySide6 import QtCore, QtGui, QtWidgets

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import project as proj                                      # noqa: E402
import saving                                               # noqa: E402
from diffview import DiffPanel                              # noqa: E402
from grid import MapGrid                                    # noqa: E402
from plot2d import Curve2D                                  # noqa: E402
from plot3d import Surface3D                                # noqa: E402
from tree import MapTree                                    # noqa: E402

ORG, APP = "Ivanbl4", "Редактор калибровок"


class MainWindow(QtWidgets.QMainWindow):

    def __init__(self, bin_path: str = "", a2l_path: str = ""):
        super().__init__()
        self.project = None
        self._preset = ""          # описание, вложенное в сборку
        self.settings = QtCore.QSettings(ORG, APP)
        self.setWindowTitle(APP)
        self.resize(1280, 800)

        self.tree = MapTree()
        self.tree.selected.connect(self.open_map)
        self.tree.groups_changed.connect(self._groups_changed)

        # Сравнение -- соседняя вкладка того же места, где дерево, а не
        # отдельное окно: список изменённых карт это тот же список карт,
        # только короче, и открывается он в ту же таблицу.
        self.diff = DiffPanel()
        self.diff.picked.connect(self.open_map)
        self.diff.mode_changed.connect(self._diff_mode)
        self.diff.cleared.connect(self._diff_off)

        self.left = QtWidgets.QTabWidget()
        self.left.addTab(self.tree, "Карты")
        self.left.addTab(self.diff, "Сравнение")

        self.grid = MapGrid()
        self.grid.edited.connect(self._after_edit)
        self.grid.currentCellChanged.connect(
            lambda r, c, pr, pc: self.curve.set_row(r))

        # Графики лежат под таблицей, а не в отдельном окне и не вкладками:
        # смотреть на кривую отдельно от чисел бессмысленно -- правку
        # делают в таблице, а форму проверяют тут же, не отводя глаз.
        # Рядом, а не вкладками, ещё и потому, что поверхности нужна
        # примерно квадратная область: в широкой полосе она вписывается
        # по высоте и висит посередине, оставляя пустые поля по бокам.
        self.curve = Curve2D()
        self.surface = Surface3D()
        self.plots = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.plots.addWidget(self.curve)
        self.plots.addWidget(self.surface)
        self.plots.setStretchFactor(0, 3)
        self.plots.setStretchFactor(1, 2)
        self.plots.setSizes([620, 420])

        self.info = QtWidgets.QLabel("Карта не выбрана")
        self.info.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.info.setWordWrap(True)
        self.info.setStyleSheet("padding:4px; color:#444;")

        vsplit = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        vsplit.addWidget(self.grid)
        vsplit.addWidget(self.plots)
        vsplit.setStretchFactor(0, 3)
        vsplit.setStretchFactor(1, 2)
        vsplit.setSizes([460, 300])
        self.vsplit = vsplit

        right = QtWidgets.QWidget()
        rl = QtWidgets.QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(2)
        rl.addWidget(vsplit, 1)
        rl.addWidget(self.info)

        split = QtWidgets.QSplitter()
        split.addWidget(self.left)
        split.addWidget(right)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([320, 960])
        self.setCentralWidget(split)

        self._build_actions()
        self._build_toolbar()
        self._build_menu()
        self.statusBar().showMessage("Откройте прошивку и описание A2L")
        self._restore_geometry()

        if bin_path and a2l_path:
            self.load(a2l_path, bin_path)

    # -- действия --------------------------------------------------------

    def _build_actions(self) -> None:
        S = QtGui.QKeySequence
        self.act_open = QtGui.QAction("Открыть...", self, shortcut=S.Open,
                                      triggered=self.ask_open)
        self.act_bin = QtGui.QAction("Сменить прошивку...", self,
                                     triggered=self.ask_bin)
        self.act_diff = QtGui.QAction("Сравнить с прошивкой...", self,
                                      triggered=self.ask_diff)
        self.act_save = QtGui.QAction("Сохранить", self, shortcut=S.Save,
                                      triggered=self.save)
        self.act_save_as = QtGui.QAction("Сохранить как...", self,
                                         shortcut=S.SaveAs,
                                         triggered=self.save_as)
        self.act_undo = QtGui.QAction("Отменить", self, shortcut=S.Undo,
                                      triggered=self.undo)
        self.act_redo = QtGui.QAction("Повторить", self, shortcut=S.Redo,
                                      triggered=self.redo)
        self.act_find = QtGui.QAction("Найти карту", self, shortcut=S.Find,
                                      triggered=self.focus_search)
        self.act_shade = QtGui.QAction("Заливка по значению", self,
                                       checkable=True, checked=True,
                                       triggered=self._toggle_shading)
        self.act_group_auto = QtGui.QAction(
            "Группировка из описания", self, checkable=True, checked=True,
            triggered=lambda: self._set_group_mode("auto"))
        self.act_group_user = QtGui.QAction(
            "Своя группировка", self, checkable=True,
            triggered=lambda: self._set_group_mode("user"))
        g = QtGui.QActionGroup(self)
        g.addAction(self.act_group_auto)
        g.addAction(self.act_group_user)
        self.act_plots = QtGui.QAction("Графики", self, checkable=True,
                                       checked=True,
                                       triggered=self._toggle_plots)
        self.act_quit = QtGui.QAction("Выход", self, shortcut=S.Quit,
                                      triggered=self.close)
        for a in (self.act_save, self.act_save_as, self.act_bin,
                  self.act_diff):
            a.setEnabled(False)

    def _build_toolbar(self) -> None:
        tb = self.addToolBar("Правка")
        tb.setMovable(False)
        tb.addAction(self.act_open)
        tb.addAction(self.act_save)
        tb.addSeparator()
        tb.addAction(self.act_undo)
        tb.addAction(self.act_redo)
        tb.addSeparator()

        self.value = QtWidgets.QDoubleSpinBox()
        self.value.setDecimals(3)
        self.value.setRange(-1e6, 1e6)
        self.value.setValue(1.0)
        self.value.setFixedWidth(100)
        self.value.setToolTip("значение для операции над выделением")
        tb.addWidget(QtWidgets.QLabel(" значение "))
        tb.addWidget(self.value)

        for label, kind, tip in (
                ("=", "=", "поставить значение"),
                ("+", "+", "прибавить"),
                ("−", "-", "вычесть"),
                ("×", "*", "умножить"),
                ("%", "%", "изменить на столько процентов"),
                ("↕", "raw", "прибавить столько шагов сетки (сырое)"),
                ("протянуть", "interp", "линейно между краями выделения"),
                ("сгладить", "smooth", "усреднить с соседями"),
        ):
            b = QtWidgets.QToolButton()
            b.setText(label)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, k=kind: self.run_op(k))
            tb.addWidget(b)

        tb.addSeparator()
        self.chk_csum = QtWidgets.QCheckBox("пересчитывать суммы")
        self.chk_csum.setChecked(True)
        self.chk_csum.setToolTip(
            "Без пересчёта блок не примет файл. Выключать только если "
            "сумму правит другой инструмент.")
        self.chk_csum.toggled.connect(self._toggle_csum)
        tb.addWidget(self.chk_csum)

    def _build_menu(self) -> None:
        m = self.menuBar().addMenu("Файл")
        m.addAction(self.act_open)
        self.menu_recent = m.addMenu("Недавние")
        self._fill_recent()
        m.addAction(self.act_bin)
        m.addAction(self.act_diff)
        m.addSeparator()
        m.addAction(self.act_save)
        m.addAction(self.act_save_as)
        m.addSeparator()
        m.addAction(self.act_quit)

        m = self.menuBar().addMenu("Правка")
        m.addAction(self.act_undo)
        m.addAction(self.act_redo)
        m.addSeparator()
        m.addAction(self.act_find)

        m = self.menuBar().addMenu("Вид")
        m.addAction(self.act_shade)
        m.addAction(self.act_plots)
        m.addSeparator()
        m.addAction(self.act_group_auto)
        m.addAction(self.act_group_user)

    # -- открытие и сохранение -------------------------------------------

    def preset_a2l(self, path: str) -> None:
        """
        Подставить описание, вложенное в сборку.

        В .exe описание Spectra лежит внутри, и заставлять человека искать
        его на диске бессмысленно: файла там может не быть вовсе. Окно
        открывается с готовым описанием, остаётся выбрать прошивку. Чужое
        описание при этом никуда не девается -- «Файл → Открыть» как было.
        """
        self._preset = path
        self.statusBar().showMessage(
            "Описание Spectra внутри программы. Осталось открыть прошивку: "
            "Файл → Открыть")

    def ask_open(self) -> None:
        start = self._preset or self._last_dir()
        a2l, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Описание A2L", start, "ASAP2 (*.a2l);;Все (*)")
        if not a2l:
            return
        binp, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Прошивка", os.path.dirname(a2l),
            "Образы (*.bin *.ori *.mod);;Все (*)")
        if not binp:
            return
        self.load(a2l, binp)

    def ask_bin(self) -> None:
        if not self.project:
            return
        binp, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Другая прошивка", os.path.dirname(self.project.bin_path),
            "Образы (*.bin *.ori *.mod);;Все (*)")
        if binp:
            self.project.reload_bin(binp)
            self.tree.set_project(self.project)
            self.grid.setRowCount(0)
            self.grid.setColumnCount(0)
            self._update_title()

    # -- недавние файлы ---------------------------------------------------

    def _recent(self) -> list:
        raw = self.settings.value("recent") or []
        if isinstance(raw, str):
            raw = [raw]
        out = []
        for item in raw:
            pair = item.split("|", 1)
            if len(pair) == 2 and all(os.path.exists(x) for x in pair):
                out.append((pair[0], pair[1]))
        return out

    def _remember(self, a2l: str, binp: str) -> None:
        items = [(a2l, binp)] + [p for p in self._recent() if p != (a2l, binp)]
        self.settings.setValue("recent",
                               ["%s|%s" % p for p in items[:8]])
        self._fill_recent()

    def _fill_recent(self) -> None:
        self.menu_recent.clear()
        items = self._recent()
        if not items:
            act = self.menu_recent.addAction("пусто")
            act.setEnabled(False)
            return
        for a2l, binp in items:
            title = "%s  —  %s" % (os.path.basename(binp),
                                   os.path.basename(a2l))
            self.menu_recent.addAction(
                title, lambda _=False, a=a2l, b=binp: self.load(a, b))

    # -- группировка ------------------------------------------------------

    def _set_group_mode(self, mode: str) -> None:
        if not self.project:
            return
        self.project.group_mode = mode
        self.project.save_project_file()
        self.tree.set_project(self.project)
        self.tree.mark_changed(self.project.changed_maps())
        self.statusBar().showMessage(
            "Группировка: " + ("своя" if mode == "user" else "из описания"))

    def _groups_changed(self) -> None:
        self.tree.mark_changed(self.project.changed_maps())
        n = sum(len(v) for v in self.project.user_groups.values())
        self.statusBar().showMessage(
            "Своих групп: %d, в них карт: %d%s"
            % (len(self.project.user_groups), n,
               "" if self.project.group_mode == "user"
               else "  (показать: Вид -> Своя группировка)"))

    def ask_diff(self) -> None:
        self.left.setCurrentWidget(self.diff)
        self.diff.ask_file()

    def _diff_mode(self, mode: str) -> None:
        """Переключили показ при сравнении -- перерисовать таблицу."""
        self._apply_compare(mode)
        self.statusBar().showMessage("Сравнение: в таблице " + {
            "this": "значения этой прошивки",
            "other": "значения второй прошивки, правка запрещена",
            "delta": "разница (вторая минус эта), правка запрещена",
        }[mode])

    def _diff_off(self) -> None:
        self.grid.set_compare(b"", "this", ())
        self.curve.set_compare(b"")
        self.statusBar().showMessage("Сравнение закрыто")

    def _apply_compare(self, mode: str = "") -> None:
        mode = mode or self.diff.mode.currentData() or "this"
        if not self.diff.other or not self.grid.map_name:
            return
        self.grid.set_compare(self.diff.other, mode,
                              self.diff.changed_cells(self.grid.map_name))
        self.curve.set_compare(self.diff.other)

    def load(self, a2l_path: str, bin_path: str) -> None:
        try:
            self.project = proj.Project.open(a2l_path, bin_path)
        except Exception as exc:                            # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, "Не открылось", str(exc))
            return
        self.settings.setValue("last_dir", os.path.dirname(bin_path))
        self.settings.setValue("last_a2l", a2l_path)
        self.settings.setValue("last_bin", bin_path)
        self._remember(a2l_path, bin_path)
        self.chk_csum.setChecked(self.project.fix_checksums)
        (self.act_group_user if self.project.group_mode == "user"
         else self.act_group_auto).setChecked(True)
        self.tree.set_project(self.project)
        self.diff.set_project(self.project)
        for a in (self.act_save, self.act_save_as, self.act_bin,
                  self.act_diff):
            a.setEnabled(True)
        self._update_title()
        t = self.project.csum_table
        self.statusBar().showMessage(
            "%d карт, адресация: вычитаем 0x%X, таблица сумм %s"
            % (len(self.project.layouts), self.project.am.subtract,
               ("0x%05X" % t) if t >= 0 else "не найдена"))

    def save(self) -> None:
        if self.project:
            self._save_to(self.project.bin_path)

    def save_as(self) -> None:
        if not self.project:
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Сохранить прошивку", self.project.bin_path,
            "Образы (*.bin);;Все (*)")
        if path:
            self._save_to(path)

    def _save_to(self, path: str) -> None:
        self.project.fix_checksums = self.chk_csum.isChecked()
        try:
            res: saving.SaveResult = self.project.save(path)
        except Exception as exc:                            # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, "Не сохранилось", str(exc))
            return
        msg = "Сохранено: %s" % os.path.basename(path)
        if res.table >= 0:
            msg += "; сумм исправлено %d из %d" % (res.fixed, res.records)
        if res.backup:
            msg += "; копия оригинала " + os.path.basename(res.backup)
        if res.note:
            msg += "; " + res.note
        self.statusBar().showMessage(msg)
        self._update_title()

    # -- работа ----------------------------------------------------------

    def open_map(self, name: str) -> None:
        if not self.project:
            return
        self.grid.set_map(self.project, name)
        self.curve.set_map(self.project, name)
        self.surface.set_map(self.project, name)
        self._apply_compare()
        L = self.project.layout(name)
        bits = ["<b>%s</b>" % name,
                "%s %dx%d" % (L.ctype, L.nx, L.ny),
                "данные 0x%05X" % L.data_off,
                "%s%d" % ("знаковый " if L.signed else "беззнаковый ",
                          L.width * 8),
                "шаг %g %s" % (L.factor, L.unit or "")]
        if L.header_off >= 0:
            bits.append("заголовок 0x%05X" % L.header_off)
        txt = " &nbsp;|&nbsp; ".join(bits)
        d = self.project.desc(name)
        if d:
            txt += "<br>" + d
        if L.note:
            txt += "<br><i>%s</i>" % L.note
        # Перекрытие -- не мелочь: правка этой карты изменит и ту, что
        # делит с ней байты. Ничего не запрещаем, но говорим прямо.
        over = self.project.overlaps(name)
        if over:
            txt += ('<br><span style="color:#9b1c1c"><b>делит байты с: %s'
                    '</b> — правка здесь изменит и её</span>'
                    % ", ".join(over[:6]))
        self.info.setText(txt)

    def run_op(self, kind: str) -> None:
        if not self.project or self.grid.lay is None:
            return
        msg = self.grid.run_op(kind, self.value.value())
        self.statusBar().showMessage(msg)

    def _after_edit(self, name: str) -> None:
        self.tree.mark_changed(self.project.changed_maps())
        self.curve.refresh()
        self.surface.refresh()
        self._update_title()

    def undo(self) -> None:
        if not self.project:
            return
        ed = self.project.undo()
        self._after_history(ed, "Отменено")

    def redo(self) -> None:
        if not self.project:
            return
        ed = self.project.redo()
        self._after_history(ed, "Повторено")

    def _after_history(self, ed, word: str) -> None:
        if ed is None:
            self.statusBar().showMessage("Нечего " + word.lower())
            return
        if self.grid.map_name != ed.map_name:
            self.tree.select_map(ed.map_name)
            self.open_map(ed.map_name)
        self.grid.refresh()
        self.curve.refresh()
        self.surface.refresh()
        self.tree.mark_changed(self.project.changed_maps())
        self._update_title()
        self.statusBar().showMessage("%s: %s на %s (%d ячеек)"
                                     % (word, ed.title, ed.map_name, ed.count))

    def focus_search(self) -> None:
        self.tree.search.setFocus()
        self.tree.search.selectAll()

    def _toggle_shading(self, on: bool) -> None:
        self.grid.shading = on
        self.grid.refresh()

    def _toggle_plots(self, on: bool) -> None:
        self.plots.setVisible(on)

    def _toggle_csum(self, on: bool) -> None:
        if self.project:
            self.project.fix_checksums = on

    # -- мелочи ----------------------------------------------------------

    def _last_dir(self) -> str:
        return self.settings.value("last_dir", os.getcwd())

    def _update_title(self) -> None:
        if not self.project:
            self.setWindowTitle(APP)
            return
        star = "* " if self.project.dirty else ""
        self.setWindowTitle("%s%s -- %s" % (
            star, os.path.basename(self.project.bin_path), APP))
        self.act_undo.setEnabled(self.project.history.can_undo)
        self.act_redo.setEnabled(self.project.history.can_redo)

    def _restore_geometry(self) -> None:
        g = self.settings.value("geometry")
        if g:
            self.restoreGeometry(g)
        v = self.settings.value("vsplit")
        if v:
            self.vsplit.restoreState(v)

    def closeEvent(self, ev) -> None:
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("vsplit", self.vsplit.saveState())
        if self.project and self.project.dirty:
            r = QtWidgets.QMessageBox.question(
                self, "Есть несохранённые правки",
                "Сохранить перед выходом?",
                QtWidgets.QMessageBox.StandardButton.Save
                | QtWidgets.QMessageBox.StandardButton.Discard
                | QtWidgets.QMessageBox.StandardButton.Cancel)
            if r == QtWidgets.QMessageBox.StandardButton.Cancel:
                ev.ignore()
                return
            if r == QtWidgets.QMessageBox.StandardButton.Save:
                self.save()
        ev.accept()
