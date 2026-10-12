#!/usr/bin/env python3
"""
Окно «Смесь по УДК».

Слева -- откуда брать и как считать: логи, каналы, что править, пороги
отбора. Справа -- что вышло: поправка по узлам, лямбда на полном газе по
оборотам и полный отчёт текстом. Внизу две кнопки, и порядок у них
намеренный: сначала «Посчитать» -- посмотреть, потом «Записать» -- в
прошивку, открытую в редакторе.

Запись идёт через историю редактора, как любая ручная правка: Ctrl+Z её
отменяет, а на диск она попадает обычным «Сохранить», с пересчётом сумм.
Окно само ничего на диск не пишет -- кроме отчёта, если попросили.

Каналы распознаются сами по именам, но каждый можно переназначить руками:
логгеры называют параметры как хотят, и угадывать до конца нельзя.
"""

from __future__ import annotations

import os
import sys

from PySide6 import QtCore, QtGui, QtWidgets

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logio                                                # noqa: E402
import palette                                              # noqa: E402
import udk                                                  # noqa: E402

LOG_FILTER = "Логи (*.csv *.txt *.log *.tsv);;Все (*)"


def _item(text: str, align_right: bool = True) -> QtWidgets.QTableWidgetItem:
    it = QtWidgets.QTableWidgetItem(text)
    it.setFlags(it.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
    if align_right:
        it.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight
                            | QtCore.Qt.AlignmentFlag.AlignVCenter)
    return it


def _paint(it: QtWidgets.QTableWidgetItem, fuel: float) -> None:
    """Больше топлива -- тёплое, меньше -- синее, 25 % -- край шкалы."""
    if abs(fuel) < 0.05:
        return
    c = palette.heat(0.5 + max(-0.5, min(0.5, fuel / 50.0)))
    it.setBackground(c)
    it.setForeground(palette.text_on(c))


class UdkDialog(QtWidgets.QDialog):
    """Смесь по УДК над проектом, открытым в редакторе."""

    applied = QtCore.Signal(list)       # имена изменённых карт

    def __init__(self, project, settings: QtCore.QSettings = None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Смесь по УДК")
        self.resize(1180, 760)
        self.project = project
        self.qs = settings
        self.ecu = None
        self.wb = None
        self.result = None

        left = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(self._build_files())
        lv.addWidget(self._build_roles())
        lv.addWidget(self._build_target())
        lv.addWidget(self._build_filters())
        lv.addStretch(1)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(left)
        scroll.setMinimumWidth(420)

        self.tabs = QtWidgets.QTabWidget()
        self.tbl_nodes = QtWidgets.QTableWidget()
        self.tbl_bands = QtWidgets.QTableWidget()
        for t in (self.tbl_nodes, self.tbl_bands):
            t.verticalHeader().setVisible(False)
            t.setAlternatingRowColors(True)
        self.txt = QtWidgets.QPlainTextEdit()
        self.txt.setReadOnly(True)
        f = QtGui.QFontDatabase.systemFont(
            QtGui.QFontDatabase.SystemFont.FixedFont)
        self.txt.setFont(f)
        self.tabs.addTab(self.tbl_nodes, "Поправка")
        self.tabs.addTab(self.tbl_bands, "Полный газ по оборотам")
        self.tabs.addTab(self.txt, "Отчёт")

        self.summary = QtWidgets.QLabel(
            "Откройте лог ЭБУ. Если УДК писал свой лог -- откройте и его.")
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.summary.setStyleSheet("padding:4px;")

        right = QtWidgets.QWidget()
        rv = QtWidgets.QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.addWidget(self.summary)
        rv.addWidget(self.tabs, 1)

        split = QtWidgets.QSplitter()
        split.addWidget(scroll)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        split.setSizes([440, 740])

        self.btn_calc = QtWidgets.QPushButton("Посчитать")
        self.btn_calc.setDefault(True)
        self.btn_calc.clicked.connect(self.compute)
        self.btn_apply = QtWidgets.QPushButton("Записать в прошивку")
        self.btn_apply.setEnabled(False)
        self.btn_apply.setToolTip(
            "Правка ложится в открытую прошивку через историю редактора: "
            "отменяется Ctrl+Z, на диск попадает по «Сохранить» с "
            "пересчётом сумм.")
        self.btn_apply.clicked.connect(self.apply)
        self.btn_report = QtWidgets.QPushButton("Сохранить отчёт...")
        self.btn_report.setEnabled(False)
        self.btn_report.clicked.connect(self.save_report)
        close = QtWidgets.QPushButton("Закрыть")
        close.clicked.connect(self.close)
        bl = QtWidgets.QHBoxLayout()
        bl.addWidget(self.btn_calc)
        bl.addWidget(self.btn_apply)
        bl.addStretch(1)
        bl.addWidget(self.btn_report)
        bl.addWidget(close)

        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(split, 1)
        lay.addLayout(bl)
        self._target_changed()

    # -- построение -------------------------------------------------------

    def _build_files(self) -> QtWidgets.QGroupBox:
        g = QtWidgets.QGroupBox("Логи")
        f = QtWidgets.QGridLayout(g)
        self.ed_ecu = QtWidgets.QLineEdit()
        self.ed_ecu.setReadOnly(True)
        self.ed_ecu.setPlaceholderText("Car Scanner, Torque, любой CSV")
        b1 = QtWidgets.QPushButton("Открыть...")
        b1.clicked.connect(self.ask_ecu)
        self.ed_wb = QtWidgets.QLineEdit()
        self.ed_wb.setReadOnly(True)
        self.ed_wb.setPlaceholderText("не нужен, если УДК в логе ЭБУ")
        b2 = QtWidgets.QPushButton("Открыть...")
        b2.clicked.connect(self.ask_wb)
        b3 = QtWidgets.QToolButton()
        b3.setText("×")
        b3.setToolTip("убрать лог УДК")
        b3.clicked.connect(self.drop_wb)
        self.sp_offset = QtWidgets.QDoubleSpinBox()
        self.sp_offset.setRange(-86400, 86400)
        self.sp_offset.setDecimals(2)
        self.sp_offset.setSuffix(" с")
        self.sp_offset.setToolTip(
            "время ЭБУ = время УДК + сдвиг. «Подобрать» находит его сам: "
            "по моментам, когда после наката открыли газ и топливо "
            "вернулось -- УДК в этот миг падает из бедного упора.")
        self.btn_off = QtWidgets.QPushButton("Подобрать")
        self.btn_off.clicked.connect(self.find_offset)
        self.lb_off = QtWidgets.QLabel("")
        self.lb_off.setWordWrap(True)
        self.lb_off.setStyleSheet("color:#555;")
        f.addWidget(QtWidgets.QLabel("Лог ЭБУ"), 0, 0)
        f.addWidget(self.ed_ecu, 0, 1)
        f.addWidget(b1, 0, 2)
        f.addWidget(QtWidgets.QLabel("Лог УДК"), 1, 0)
        f.addWidget(self.ed_wb, 1, 1)
        h = QtWidgets.QHBoxLayout()
        h.addWidget(b2)
        h.addWidget(b3)
        f.addLayout(h, 1, 2)
        f.addWidget(QtWidgets.QLabel("Сдвиг УДК"), 2, 0)
        f.addWidget(self.sp_offset, 2, 1)
        f.addWidget(self.btn_off, 2, 2)
        f.addWidget(self.lb_off, 3, 0, 1, 3)
        self._wb_enabled(False)
        return g

    def _build_roles(self) -> QtWidgets.QGroupBox:
        g = QtWidgets.QGroupBox("Каналы (распознаются сами, можно поправить)")
        f = QtWidgets.QFormLayout(g)
        self.cb_role = {}
        for role, title, must in udk.ROLES:
            cb = QtWidgets.QComboBox()
            cb.setSizeAdjustPolicy(
                QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            cb.setMinimumContentsLength(18)
            self.cb_role[role] = cb
            f.addRow(title + (" *" if must else ""), cb)
        self.cb_lam = QtWidgets.QComboBox()
        for text, val in (("сам: λ или AFR по значениям", "auto"),
                          ("лямбда", "lambda"), ("AFR, бензин 14.7", "afr")):
            self.cb_lam.addItem(text, val)
        self.cb_maf = QtWidgets.QComboBox()
        for text, val in (("сам: по подписи и величине", "auto"),
                          ("г/с", "g/s"), ("кг/ч", "kg/h")):
            self.cb_maf.addItem(text, val)
        f.addRow("УДК пишет", self.cb_lam)
        f.addRow("Расход в", self.cb_maf)
        return g

    def _build_target(self) -> QtWidgets.QGroupBox:
        g = QtWidgets.QGroupBox("Что править")
        v = QtWidgets.QVBoxLayout(g)
        self.rb_kl = QtWidgets.QRadioButton(
            "Лямбду полного газа (KLLAMFA)")
        self.rb_kl.setToolTip(
            "Цель по лямбде при газе в пол, по оборотам. Ставится такой, "
            "чтобы УДК показал желаемую. Трогает только полный газ.")
        self.rb_hfm = QtWidgets.QRadioButton("Тарировку ДМРВ (HFM_LIN)")
        self.rb_hfm.setToolTip(
            "Расход по напряжению ДМРВ. Убирает ошибку измерения воздуха "
            "во всех режимах, нагрузка для УОЗ становится честной. На "
            "частичных нужна коррекция STFT в логе.")
        self.rb_kl.setChecked(True)
        self.rb_kl.toggled.connect(self._target_changed)
        h = QtWidgets.QHBoxLayout()
        h.addWidget(QtWidgets.QLabel("Желаемая λ на полном газе"))
        self.sp_want = QtWidgets.QDoubleSpinBox()
        self.sp_want.setRange(0.70, 1.05)
        self.sp_want.setDecimals(3)
        self.sp_want.setSingleStep(0.01)
        self.sp_want.setValue(0.88)
        h.addWidget(self.sp_want)
        h.addStretch(1)
        self.cb_var = QtWidgets.QComboBox()
        self.cb_var.addItem("вариант 0", 0)
        self.cb_var.addItem("вариант 1", 1)
        self.cb_var.setToolTip(
            "У KLLAMFA и WDKVLN по два варианта, какой работает -- решает "
            "блок. Считаем по выбранному, пишем в оба.")
        v.addWidget(self.rb_kl)
        v.addLayout(h)
        v.addWidget(self.rb_hfm)
        h2 = QtWidgets.QHBoxLayout()
        h2.addWidget(QtWidgets.QLabel("Цель и порог полного газа:"))
        h2.addWidget(self.cb_var)
        h2.addStretch(1)
        v.addLayout(h2)
        return g

    def _spin(self, lo, hi, val, dec, step, suffix="", tip=""):
        s = QtWidgets.QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setDecimals(dec)
        s.setSingleStep(step)
        s.setValue(val)
        if suffix:
            s.setSuffix(suffix)
        if tip:
            s.setToolTip(tip)
        return s

    def _build_filters(self) -> QtWidgets.QGroupBox:
        g = QtWidgets.QGroupBox("Отбор и пределы")
        f = QtWidgets.QFormLayout(g)
        d = udk.Settings()
        self.sp_lag = self._spin(0, 3, d.lag, 2, 0.05, " с",
                                 "УДК видит газ, сгоревший раньше: на столько "
                                 "его показания сдвигаются назад")
        self.sp_ect = self._spin(-40, 120, d.ect_min, 0, 5, " °C",
                                 "холоднее -- прогревное обогащение, не верим")
        self.sp_rate = self._spin(5, 500, d.tps_rate, 0, 5, " %/с",
                                  "быстрее -- рывок дросселя: обогащение на "
                                  "ускорение и плёнка топлива")
        self.sp_settle = self._spin(0, 5, d.settle, 1, 0.1, " с",
                                    "сколько не верить после рывка")
        self.sp_min = self._spin(0.1, 30, d.min_time, 1, 0.1, " с",
                                 "меньше данных на узел -- узел не правим")
        self.sp_rich = self._spin(0, 50, d.max_rich, 0, 1, " %",
                                  "в богатую за один проход не больше")
        self.sp_lean = self._spin(0, 50, d.max_lean, 0, 1, " %",
                                  "в бедную за один проход не больше: бедная "
                                  "на полном газе прожигает поршни")
        self.chk_fill = QtWidgets.QCheckBox("продлить на узлы без данных")
        self.chk_fill.setChecked(d.fill_empty)
        self.chk_fill.setToolTip(
            "Обороты выше последних в логе получат ту же поправку, что "
            "последний узел с данными. Без этого верх останется бедным.")
        self.chk_bts = QtWidgets.QCheckBox(
            "защита компонентов KFLBTS работала")
        self.chk_bts.setToolTip(
            "Если включена, цель берётся ещё и минимумом с KFLBTS. Когда "
            "она на самом деле включается, из лога не видно.")
        f.addRow("Задержка УДК", self.sp_lag)
        f.addRow("ОЖ не ниже", self.sp_ect)
        f.addRow("Рывок дросселя", self.sp_rate)
        f.addRow("После рывка", self.sp_settle)
        f.addRow("Данных на узел", self.sp_min)
        f.addRow("Обогащать до", self.sp_rich)
        f.addRow("Обеднять до", self.sp_lean)
        f.addRow(self.chk_fill)
        f.addRow(self.chk_bts)
        return g

    # -- логи --------------------------------------------------------------

    def _dir(self) -> str:
        if self.qs is not None:
            return self.qs.value("udk_dir", os.getcwd())
        return os.getcwd()

    def _remember_dir(self, path: str) -> None:
        if self.qs is not None:
            self.qs.setValue("udk_dir", os.path.dirname(path))

    def ask_ecu(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Лог ЭБУ", self._dir(), LOG_FILTER)
        if path:
            self.load_ecu(path)

    def ask_wb(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Лог УДК", self._dir(), LOG_FILTER)
        if path:
            self.load_wb(path)

    def load_ecu(self, path: str) -> bool:
        try:
            self.ecu = logio.read_log(path)
        except Exception as exc:                            # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "Лог не прочитан", str(exc))
            return False
        self._remember_dir(path)
        self.ed_ecu.setText(path)
        self._fill_roles()
        self._say("Лог ЭБУ: %d каналов, %.0f с%s" % (
            len(self.ecu.channels), self.ecu.duration,
            (". " + self.ecu.note) if self.ecu.note else ""))
        return True

    def load_wb(self, path: str) -> bool:
        try:
            self.wb = logio.read_log(path)
        except Exception as exc:                            # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "Лог не прочитан", str(exc))
            return False
        self._remember_dir(path)
        self.ed_wb.setText(path)
        self._wb_enabled(True)
        self._fill_roles()
        if self.ecu is not None:
            self.find_offset()
        return True

    def drop_wb(self) -> None:
        self.wb = None
        self.ed_wb.clear()
        self.lb_off.clear()
        self._wb_enabled(False)
        self._fill_roles()

    def _wb_enabled(self, on: bool) -> None:
        self.sp_offset.setEnabled(on)
        self.btn_off.setEnabled(on)

    def _fill_roles(self) -> None:
        auto = (udk.detect_roles(self.ecu, self.wb) if self.ecu is not None
                else {})
        for role, cb in self.cb_role.items():
            cb.blockSignals(True)
            cb.clear()
            cb.addItem("—", None)
            for src, log in (("ecu", self.ecu), ("wb", self.wb)):
                if log is None:
                    continue
                pre = "ЭБУ: " if src == "ecu" else "УДК: "
                for name, ch in log.channels.items():
                    cb.addItem(pre + ch.label, (src, name))
            want = auto.get(role)
            if want:
                for i in range(cb.count()):
                    if cb.itemData(i) == want:
                        cb.setCurrentIndex(i)
                        break
            cb.blockSignals(False)

    def roles(self) -> dict:
        out = {}
        for role, cb in self.cb_role.items():
            d = cb.currentData()
            out[role] = tuple(d) if d else None
        return out

    # -- расчёт --------------------------------------------------------------

    def settings(self) -> udk.Settings:
        return udk.Settings(
            target="kllamfa" if self.rb_kl.isChecked() else "hfm",
            lam_want=self.sp_want.value(), lag=self.sp_lag.value(),
            offset=self.sp_offset.value(), ect_min=self.sp_ect.value(),
            tps_rate=self.sp_rate.value(), settle=self.sp_settle.value(),
            min_time=self.sp_min.value(), max_rich=self.sp_rich.value(),
            max_lean=self.sp_lean.value(),
            fill_empty=self.chk_fill.isChecked(),
            bts=self.chk_bts.isChecked(),
            lam_kind=self.cb_lam.currentData(),
            maf_unit=self.cb_maf.currentData(),
            variant=self.cb_var.currentData())

    def find_offset(self) -> None:
        if self.ecu is None or self.wb is None:
            return
        off, how = udk.find_offset(self.ecu, self.wb, self.roles(),
                                   self.settings())
        if off is None:
            self.lb_off.setText("Не нашёл: " + how)
            return
        self.sp_offset.setValue(off)
        self.lb_off.setText("Сдвиг %+.2f с -- %s" % (off, how))

    def compute(self) -> None:
        if self.ecu is None:
            self._say("Сначала откройте лог ЭБУ")
            return
        st = self.settings()
        try:
            fm = udk.FuelModel.from_project(self.project, st.variant)
            res = udk.analyze(fm, self.ecu, self.wb, self.roles(), st)
        except Exception as exc:                            # noqa: BLE001
            self.result = None
            self.btn_apply.setEnabled(False)
            self._say("<b>Не посчиталось:</b> %s" % exc)
            return
        self.result = res
        self._show(res)
        self.btn_apply.setEnabled(res.changed)
        self.btn_report.setEnabled(True)

    def _show(self, res: udk.Result) -> None:
        st = res.settings
        bits = ["Годных %.0f с из %.0f, в поправку %.0f с."
                % (res.accepted, res.total, len(res.used) * st.dt)]
        if res.bands:
            worst = max(res.bands, key=lambda b: b.err)
            bits.append("Полный газ: хуже всего %d–%d об/мин, λ %.3f при "
                        "цели %.3f (%+.1f %% воздуха мимо счёта)."
                        % (worst.lo, worst.hi, worst.lam, worst.lcmd,
                           (worst.err - 1) * 100))
        for w in res.warnings:
            bits.append('<span style="color:#9b1c1c">%s</span>' % w)
        if not res.changed:
            bits.append("<b>Менять нечего</b>: по данным поправка нулевая "
                        "или данных мало.")
        self.summary.setText("<br>".join(bits))
        self._fill_nodes(res)
        self._fill_bands(res)
        self.txt.setPlainText(udk.report(res))

    def _fill_nodes(self, res: udk.Result) -> None:
        t = self.tbl_nodes
        hfm = res.target == "hfm"
        heads = (["Узел", "с данных", "λ УДК", "цель", "ошибка воздуха",
                  "множитель", "топлива", "откуда"] if hfm else
                 ["Обороты", "с данных", "λ УДК", "цель", "ошибка воздуха",
                  "было", "стало", "топлива", "откуда"])
        nodes = [n for n in res.nodes
                 if not (hfm and n.status == "нет данных" and n.new == n.old)]
        t.clear()
        t.setColumnCount(len(heads))
        t.setHorizontalHeaderLabels(heads)
        t.setRowCount(len(nodes))
        for r, n in enumerate(nodes):
            has = n.time > 0
            fuel = ((n.new - 1) * 100 if hfm
                    else (n.old / n.new - 1) * 100 if n.new else 0.0)
            row = [n.label, "%.1f" % n.time,
                   "%.3f" % n.lam if has else "–",
                   "%.3f" % n.lcmd if has else "–",
                   "%+.1f %%" % ((n.err - 1) * 100) if has else "–"]
            row += (["%.3f" % n.new] if hfm
                    else ["%.3f" % n.old, "%.3f" % n.new])
            row += ["%+.1f %%" % fuel, n.status]
            for c, text in enumerate(row):
                it = _item(text, c not in (0, len(row) - 1))
                if c == len(row) - 2:
                    _paint(it, fuel)
                t.setItem(r, c, it)
        t.resizeColumnsToContents()
        t.horizontalHeader().setStretchLastSection(True)

    def _fill_bands(self, res: udk.Result) -> None:
        t = self.tbl_bands
        heads = ["Обороты", "с", "λ средняя", "λ мин", "λ макс", "цель",
                 "ошибка воздуха"]
        t.clear()
        t.setColumnCount(len(heads))
        t.setHorizontalHeaderLabels(heads)
        t.setRowCount(len(res.bands))
        for r, b in enumerate(res.bands):
            row = ["%.0f–%.0f" % (b.lo, b.hi), "%.1f" % b.time,
                   "%.3f" % b.lam, "%.3f" % b.lmin, "%.3f" % b.lmax,
                   "%.3f" % b.lcmd, "%+.1f %%" % ((b.err - 1) * 100)]
            for c, text in enumerate(row):
                it = _item(text, c != 0)
                if c == 6:
                    _paint(it, (b.err - 1) * 100)
                t.setItem(r, c, it)
        t.resizeColumnsToContents()
        t.horizontalHeader().setStretchLastSection(True)

    # -- запись ----------------------------------------------------------------

    def apply(self) -> None:
        if self.result is None:
            return
        eds = udk.apply(self.project, self.result)
        self.btn_apply.setEnabled(False)
        if not eds:
            self._say("Прошивка уже такая -- менять нечего")
            return
        names = [e.map_name for e in eds]
        self._say("Записано в открытую прошивку: %s. Сохраните её "
                  "(Ctrl+S) -- суммы пересчитаются. После заливки сбросьте "
                  "адаптацию и снимите новый лог."
                  % ", ".join("%s (%d яч.)" % (e.map_name, e.count)
                              for e in eds))
        self.applied.emit(names)

    def save_report(self) -> None:
        if self.result is None:
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Отчёт", os.path.join(self._dir(), "смесь_по_удк.txt"),
            "Текст (*.txt);;Все (*)")
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(udk.report(self.result) + "\n")
            self._say("Отчёт сохранён: " + os.path.basename(path))

    # -- мелочи ------------------------------------------------------------------

    def set_project(self, project) -> None:
        """Сменилась прошивка в редакторе -- старый расчёт уже не про неё."""
        self.project = project
        self.result = None
        self.btn_apply.setEnabled(False)

    def _target_changed(self) -> None:
        self.sp_want.setEnabled(self.rb_kl.isChecked())
        self.result = None
        self.btn_apply.setEnabled(False)

    def _say(self, html: str) -> None:
        self.summary.setText(html)
