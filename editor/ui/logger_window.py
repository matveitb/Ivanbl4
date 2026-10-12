#!/usr/bin/env python3
"""
Окно «Снять лог»: ELM и УДК в один файл, и сразу видно, что творится.

Слева -- к чему подключаться: порт адаптера (COM, Bluetooth-COM, Wi-Fi
или имитатор), порт УДК и его протокол, куда писать файл. Справа --
приборы и таблицы, которые наполняются по ходу езды: что показывает УДК
на полном газе по оборотам и сколько секунд набралось в каждом узле
тарировки ДМРВ. Когда набралось -- «Стоп» и «Открыть в расчёте».

Подключение и опрос идут в своих потоках, окно только раз в треть
секунды заглядывает в их состояние. Так окно не замирает, пока K-линия
поднимается (это секунды), и не зависит от того, насколько медленный
попался адаптер.
"""

from __future__ import annotations

import datetime
import os
import queue
import sys
import threading
import time

from PySide6 import QtCore, QtGui, QtWidgets

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import elm as E                                             # noqa: E402
import livelog                                              # noqa: E402
import obd                                                  # noqa: E402
import palette                                              # noqa: E402
import udk                                                  # noqa: E402
import wideband                                             # noqa: E402

DEMO = "demo"
WIFI = "wifi"
NONE = "none"

PROTOCOLS = [("0", "сам (ATSP0)"), ("5", "KWP2000 быстрый старт (ATSP5)"),
             ("4", "KWP2000 5 бод (ATSP4)"), ("3", "ISO 9141-2 (ATSP3)")]
BAUDS = [0, 38400, 115200, 9600, 230400, 57600, 500000]

GAUGES = [
    ("rpm", "Обороты", "%.0f"),
    ("maf", "Расход, г/с", "%.1f"),
    ("tps", "Дроссель, %", "%.0f"),
    ("lam", "λ УДК", "%.3f"),
    ("lcmd", "Цель λ", "%.3f"),
    ("err", "Ошибка воздуха", "%+.1f %%"),
    ("stft", "STFT, %", "%+.1f"),
    ("ltft", "LTFT, %", "%+.1f"),
    ("ect", "ОЖ, °C", "%.0f"),
    ("loop", "Контур", "%s"),
]


def default_log_path() -> str:
    base = os.path.join(os.path.expanduser("~"), "Documents")
    if not os.path.isdir(base):
        base = os.path.expanduser("~")
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return os.path.join(base, "udk_logs", "лог_%s.csv" % stamp)


class Session:
    """Подключение и опрос в фоне. Окно читает только поля и очередь."""

    def __init__(self, cfg: dict, fm=None):
        self.cfg = cfg
        self.fm = fm
        self.msgs: queue.Queue = queue.Queue()
        self.logger = None
        self.elm = None
        self.wt = None
        self.cancelled = False
        self.state = "connecting"      # connecting | running | stopped | failed
        self._th = threading.Thread(target=self._connect, daemon=True)
        self._th.start()

    def say(self, m: str) -> None:
        self.msgs.put(m)

    def _transport_elm(self):
        c = self.cfg
        if c["elm"] == DEMO:
            import fakecar
            if self.fm is None:
                raise E.ElmError("для имитатора нужна открытая прошивка")
            self.car = fakecar.FakeCar(self.fm)
            return fakecar.FakeElm(self.car, time.monotonic, realtime=True)
        if c["elm"] == WIFI:
            host, _, port = c.get("wifi", "192.168.0.10:35000").partition(":")
            self.say("Wi-Fi: %s:%s" % (host, port or "35000"))
            return E.TcpTransport(host, int(port or 35000))
        return E.connect_serial(c["elm"], c.get("baud") or None, self.say)

    def _transport_wb(self):
        c = self.cfg
        if c["wb"] == NONE:
            return None, None
        kw = {}
        if c["wb_proto"] == "ascii":
            kw = {"kind": c.get("wb_kind", "auto")}
        parser = wideband.make(c["wb_proto"], **kw)
        if c["wb"] == DEMO:
            import fakecar
            car = getattr(self, "car", None)
            if car is None:
                if self.fm is None:
                    raise E.ElmError("для имитатора УДК нужна прошивка")
                car = self.car = fakecar.FakeCar(self.fm)
            return fakecar.FakeWideband(car, time.monotonic,
                                        proto=c["wb_proto"],
                                        realtime=True), parser
        baud = c.get("wb_baud") or parser.baud
        self.say("УДК: %s, %d бод, %s" % (c["wb"], baud, parser.name))
        return E.SerialTransport(c["wb"], baud), parser

    def _connect(self) -> None:
        try:
            t = self._transport_elm()
            self.elm = E.Elm327(t)
            self.elm.init(self.cfg.get("proto", "0"), self.say)
            sup = self.elm.supported_pids()
            self.say("блок отдаёт PID: %s" % " ".join(
                "%02X" % p for p in sorted(sup)))
            miss = [obd.PIDS[p].title for p in (0x0C, 0x10, 0x11)
                    if p not in sup]
            if miss:
                self.say("ВНИМАНИЕ: блок не отдаёт " + ", ".join(miss))
            dids = livelog.probe_dids(self.elm)
            self.say("режим 22: %s" % (", ".join(
                obd.DIDS[d].title for d in sorted(dids)) if dids else
                "блок не отвечает, пишу по обычным PID"))
            self.wt, parser = self._transport_wb()
            if self.cancelled:
                raise E.ElmError("остановлено до начала записи")
            self.logger = livelog.LiveLogger(
                self.elm, self.wt, parser, path=self.cfg["path"],
                supported=sup, dids=dids)
            self.logger.start()
            if self.cancelled:          # «Стоп» нажали, пока запускались
                self.logger.stop()
                self._close_ports()
                self.state = "stopped"
                return
            self.state = "running"
            self.say("пишу в " + self.cfg["path"])
        except Exception as exc:                            # noqa: BLE001
            self.state = "failed"
            self.say("ОШИБКА: %s" % exc)
            self._close_ports()

    def _close_ports(self) -> None:
        for obj in (self.elm, self.wt):
            try:
                if obj is not None:
                    obj.close()
            except Exception:                               # noqa: BLE001
                pass

    def stop(self) -> None:
        """
        Остановить. Если подключение ещё идёт, ждать его до конца нельзя
        -- окно замрёт на десяток секунд, пока K-линия не ответит. Поэтому
        порты закрываются сразу (чтение в потоке подключения прервётся), а
        сам поток, если успеет дойти до записи, увидит отмену и остановит
        её сам.
        """
        self.cancelled = True
        if self._th.is_alive():
            self._close_ports()
            self._th.join(timeout=1.0)
        if self.logger is not None:
            self.logger.stop()
        self._close_ports()
        if self.state != "failed":
            self.state = "stopped"


class LoggerDialog(QtWidgets.QDialog):

    open_in_calc = QtCore.Signal(str)       # путь к снятому логу

    def __init__(self, project=None, settings: QtCore.QSettings = None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Снять лог: ELM + УДК")
        self.resize(1180, 760)
        self.project = project
        self.qs = settings
        self.session = None
        self.last_path = ""
        self._last_calc = 0.0

        left = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(self._build_elm())
        lv.addWidget(self._build_wb())
        lv.addWidget(self._build_file())
        self.btn_start = QtWidgets.QPushButton("Подключить и писать")
        self.btn_start.clicked.connect(self.start)
        self.btn_stop = QtWidgets.QPushButton("Стоп")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop)
        self.btn_calc = QtWidgets.QPushButton("Открыть в расчёте")
        self.btn_calc.setEnabled(False)
        self.btn_calc.setToolTip("Снятый лог -- в окно «Смесь по УДК»")
        self.btn_calc.clicked.connect(
            lambda: self.open_in_calc.emit(self.last_path))
        h = QtWidgets.QHBoxLayout()
        h.addWidget(self.btn_start)
        h.addWidget(self.btn_stop)
        lv.addLayout(h)
        lv.addWidget(self.btn_calc)
        self.messages = QtWidgets.QPlainTextEdit()
        self.messages.setReadOnly(True)
        self.messages.setMaximumBlockCount(500)
        lv.addWidget(self.messages, 1)

        right = QtWidgets.QWidget()
        rv = QtWidgets.QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.addWidget(self._build_gauges())
        self.lb_rates = QtWidgets.QLabel("")
        self.lb_rates.setStyleSheet("color:#555;")
        self.lb_rates.setWordWrap(True)
        rv.addWidget(self.lb_rates)
        self.tabs = QtWidgets.QTabWidget()
        self.tbl_bands = QtWidgets.QTableWidget()
        self.tbl_nodes = QtWidgets.QTableWidget()
        for t in (self.tbl_bands, self.tbl_nodes):
            t.verticalHeader().setVisible(False)
            t.setEditTriggers(
                QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tabs.addTab(self.tbl_bands, "Полный газ по оборотам")
        self.tabs.addTab(self.tbl_nodes, "Узлы ДМРВ: набрано")
        rv.addWidget(self.tabs, 1)

        split = QtWidgets.QSplitter()
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        split.setSizes([420, 760])
        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(split)

        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(300)
        self.timer.timeout.connect(self.tick)
        self.refresh_ports()

    # -- построение -----------------------------------------------------------

    def _build_elm(self) -> QtWidgets.QGroupBox:
        g = QtWidgets.QGroupBox("Адаптер ELM327")
        f = QtWidgets.QFormLayout(g)
        self.cb_elm = QtWidgets.QComboBox()
        self.cb_elm.setToolTip(
            "COM-порт: USB-адаптер или Bluetooth (сначала спарить в Windows, "
            "тогда он виден как COM). Адаптеры только с Bluetooth LE "
            "COM-портом не становятся -- с ними не выйдет.")
        ref = QtWidgets.QToolButton()
        ref.setText("↻")
        ref.setToolTip("обновить список портов")
        ref.clicked.connect(self.refresh_ports)
        h = QtWidgets.QHBoxLayout()
        h.addWidget(self.cb_elm, 1)
        h.addWidget(ref)
        f.addRow("Подключение", h)
        self.ed_wifi = QtWidgets.QLineEdit("192.168.0.10:35000")
        f.addRow("Wi-Fi адрес", self.ed_wifi)
        self.cb_baud = QtWidgets.QComboBox()
        for b in BAUDS:
            self.cb_baud.addItem("подобрать" if not b else str(b), b)
        f.addRow("Скорость порта", self.cb_baud)
        self.cb_proto = QtWidgets.QComboBox()
        for k, t in PROTOCOLS:
            self.cb_proto.addItem(t, k)
        f.addRow("Протокол", self.cb_proto)
        return g

    def _build_wb(self) -> QtWidgets.QGroupBox:
        g = QtWidgets.QGroupBox("УДК")
        f = QtWidgets.QFormLayout(g)
        self.cb_wb = QtWidgets.QComboBox()
        f.addRow("Порт", self.cb_wb)
        self.cb_wbp = QtWidgets.QComboBox()
        for k, t in wideband.PARSER_TITLES:
            self.cb_wbp.addItem(t, k)
        self.cb_wbp.currentIndexChanged.connect(self._wb_proto_changed)
        f.addRow("Протокол", self.cb_wbp)
        self.cb_wbb = QtWidgets.QComboBox()
        for b in (0, 9600, 19200, 38400, 57600, 115200):
            self.cb_wbb.addItem("как у протокола" if not b else str(b), b)
        f.addRow("Скорость", self.cb_wbb)
        self.cb_wbk = QtWidgets.QComboBox()
        for t, k in (("сам: λ или AFR", "auto"), ("лямбда", "lambda"),
                     ("AFR", "afr")):
            self.cb_wbk.addItem(t, k)
        f.addRow("Пишет", self.cb_wbk)
        self._wb_proto_changed()
        return g

    def _build_file(self) -> QtWidgets.QGroupBox:
        g = QtWidgets.QGroupBox("Файл лога")
        h = QtWidgets.QHBoxLayout(g)
        self.ed_path = QtWidgets.QLineEdit(default_log_path())
        b = QtWidgets.QPushButton("...")
        b.setFixedWidth(32)
        b.clicked.connect(self.ask_path)
        h.addWidget(self.ed_path, 1)
        h.addWidget(b)
        return g

    def _build_gauges(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(w)
        self.gauges = {}
        big = QtGui.QFont()
        big.setPointSize(20)
        big.setBold(True)
        for i, (key, title, _fmt) in enumerate(GAUGES):
            box = QtWidgets.QFrame()
            box.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
            bl = QtWidgets.QVBoxLayout(box)
            cap = QtWidgets.QLabel(title)
            cap.setStyleSheet("color:#555;")
            val = QtWidgets.QLabel("–")
            val.setFont(big)
            val.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            bl.addWidget(cap)
            bl.addWidget(val)
            grid.addWidget(box, i // 5, i % 5)
            self.gauges[key] = val
        return w

    # -- порты ---------------------------------------------------------------

    def refresh_ports(self) -> None:
        ports = E.list_serial_ports()
        cur_e = self.cb_elm.currentData()
        cur_w = self.cb_wb.currentData()
        self.cb_elm.clear()
        self.cb_wb.clear()
        self.cb_wb.addItem("нет: УДК не подключён", NONE)
        for dev, desc in ports:
            text = "%s  %s" % (dev, desc) if desc else dev
            self.cb_elm.addItem(text, dev)
            self.cb_wb.addItem(text, dev)
        self.cb_elm.addItem("Wi-Fi адаптер", WIFI)
        self.cb_elm.addItem("Имитатор: модель мотора", DEMO)
        self.cb_wb.addItem("Имитатор: модель мотора", DEMO)
        saved_e = self.qs.value("log_elm", "") if self.qs else ""
        saved_w = self.qs.value("log_wb", "") if self.qs else ""
        for cb, want in ((self.cb_elm, cur_e or saved_e),
                         (self.cb_wb, cur_w or saved_w)):
            i = cb.findData(want) if want else -1
            if i >= 0:
                cb.setCurrentIndex(i)

    def _wb_proto_changed(self) -> None:
        self.cb_wbk.setEnabled(self.cb_wbp.currentData() == "ascii")

    def ask_path(self) -> None:
        p, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Куда писать лог", self.ed_path.text(),
            "CSV (*.csv);;Все (*)")
        if p:
            self.ed_path.setText(p)

    # -- работа ----------------------------------------------------------------

    def config(self) -> dict:
        return {
            "elm": self.cb_elm.currentData(),
            "wifi": self.ed_wifi.text().strip(),
            "baud": self.cb_baud.currentData(),
            "proto": self.cb_proto.currentData(),
            "wb": self.cb_wb.currentData() or NONE,
            "wb_proto": self.cb_wbp.currentData(),
            "wb_baud": self.cb_wbb.currentData(),
            "wb_kind": self.cb_wbk.currentData(),
            "path": self.ed_path.text().strip() or default_log_path(),
        }

    def _fm(self):
        if self.project is None:
            return None
        try:
            return udk.FuelModel.from_project(self.project)
        except Exception:                                   # noqa: BLE001
            return None

    def start(self) -> None:
        if self.session is not None and self.session.state in (
                "connecting", "running"):
            return
        cfg = self.config()
        if not cfg["elm"]:
            self.say("Нет ни одного порта. Подключите адаптер и нажмите ↻")
            return
        if os.path.exists(cfg["path"]):
            cfg["path"] = default_log_path()
            self.ed_path.setText(cfg["path"])
        if self.qs is not None:
            self.qs.setValue("log_elm", cfg["elm"])
            self.qs.setValue("log_wb", cfg["wb"])
        self.fm = self._fm()
        self.session = Session(cfg, self.fm)
        self.last_path = cfg["path"]
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_calc.setEnabled(False)
        self.say("подключаюсь...")
        self.timer.start()

    def stop(self) -> None:
        if self.session is None:
            return
        self.session.stop()
        self.tick()
        self.timer.stop()
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        ok = os.path.exists(self.last_path) and os.path.getsize(
            self.last_path) > 40
        self.btn_calc.setEnabled(ok)
        if ok:
            self.say("лог записан: %s" % self.last_path)
            self.ed_path.setText(default_log_path())

    def say(self, m: str) -> None:
        self.messages.appendPlainText(
            time.strftime("%H:%M:%S ") + m)

    def tick(self) -> None:
        s = self.session
        if s is None:
            return
        while True:
            try:
                self.say(s.msgs.get_nowait())
            except queue.Empty:
                break
        if s.state == "failed":
            self.timer.stop()
            self.btn_start.setEnabled(True)
            self.btn_stop.setEnabled(False)
            return
        L = s.logger
        if L is None:
            return
        if L.error:
            self.say(L.error)
            L.error = ""
        self._gauges(L)
        now = time.monotonic()
        if now - self._last_calc > 2.0:
            self._last_calc = now
            self._tables(L)

    # -- приборы -------------------------------------------------------------------

    def _gauges(self, L) -> None:
        last = L.latest()
        now = L.clock()

        def get(name, fresh=3.0):
            v = last.get(name)
            return v[1] if v and now - v[0] <= fresh else None

        maf = get(obd.PIDS[0x10].name)
        if maf is None:                      # режим 22: кг/ч в полном разрешении
            kgh = get(obd.DIDS[0x1285].name)
            maf = kgh / udk.KGH_PER_GS if kgh is not None else None
        vals = {
            "rpm": get(obd.PIDS[0x0C].name),
            "maf": maf,
            "tps": get(obd.PIDS[0x11].name),
            "lam": get(livelog.LAMBDA_NAME, 1.0),
            "stft": get(obd.PIDS[0x06].name, 5.0),
            "ltft": get(obd.PIDS[0x07].name, 30.0),
            "ect": get(obd.PIDS[0x05].name, 60.0),
        }
        fs = get(obd.PIDS[0x03].name, 10.0)
        vals["loop"] = {1: "ждёт", 2: "замкнут", 4: "разомкнут",
                        8: "авария", 16: "замкнут*"}.get(
            int(fs) if fs is not None else -1, None)
        cl = get(obd.DIDS[0x1216].name, 10.0)
        if vals["loop"] is None and cl is not None:
            vals["loop"] = "замкнут" if cl else "разомкнут"
        fm = getattr(self, "fm", None)
        if fm is not None and vals["rpm"] and vals["tps"] is not None:
            ml = (vals["maf"] or 0) * udk.KGH_PER_GS
            lcmd, _fl = fm.lam_cmd(vals["rpm"], vals["tps"],
                                   fm.rl(vals["rpm"], ml))
            vals["lcmd"] = lcmd
            if vals["lam"] is not None:
                trims = ((1 + (vals["stft"] or 0) / 100)
                         * (1 + (vals["ltft"] or 0) / 100))
                vals["err"] = (vals["lam"] * trims / lcmd - 1) * 100
        for key, _title, fmt in GAUGES:
            v = vals.get(key)
            lab = self.gauges[key]
            lab.setText("–" if v is None else (fmt % v))
            if key == "lam" and v is not None:
                c = palette.heat(0.5 + max(-0.5, min(0.5, (v - 1.0) * 3)))
                lab.setStyleSheet("background:%s; color:%s; border-radius:4px;"
                                  % (c.name(), palette.text_on(c).name()))
        r = L.rates()
        wb = L.wb
        parts = ["%s %.1f Гц" % (obd.PIDS[p].title, r.get(obd.PIDS[p].name, 0))
                 for p in (0x0C, 0x10, 0x11)
                 if r.get(obd.PIDS[p].name) is not None]
        adc = obd.DIDS[0x1235]
        if r.get(adc.name) is not None:
            parts.append("%s %.1f Гц" % (adc.title, r[adc.name]))
        if wb is not None:
            parts.append("УДК %.1f Гц, %s" % (r.get(livelog.LAMBDA_NAME, 0),
                                              wb.status or "нет данных"))
        st = L.elm.stats
        parts.append("ответов %d из %d, задержка %.0f мс" % (
            st.answered, st.requests, st.latency * 1000))
        parts.append("записано %.0f с" % now)
        self.lb_rates.setText(" · ".join(parts))

    def _tables(self, L) -> None:
        fm = getattr(self, "fm", None)
        if fm is None or L.wb is None:
            return
        log = L.rec.snapshot()
        roles = udk.detect_roles(log, None)
        if not all(roles[r] for r in ("rpm", "maf", "tps", "lam")):
            return
        try:
            res = udk.analyze(fm, log, None, roles,
                              udk.Settings(target="hfm"))
        except Exception:                                   # noqa: BLE001
            return
        t = self.tbl_bands
        heads = ["Обороты", "с", "λ средняя", "цель", "ошибка воздуха"]
        t.setColumnCount(len(heads))
        t.setHorizontalHeaderLabels(heads)
        t.setRowCount(len(res.bands))
        for r, b in enumerate(res.bands):
            row = ["%.0f–%.0f" % (b.lo, b.hi), "%.1f" % b.time,
                   "%.3f" % b.lam, "%.3f" % b.lcmd,
                   "%+.1f %%" % ((b.err - 1) * 100)]
            for c, text in enumerate(row):
                it = QtWidgets.QTableWidgetItem(text)
                if c == 4:
                    col = palette.heat(0.5 + max(-0.5, min(0.5,
                                                           (b.err - 1) * 2)))
                    it.setBackground(col)
                    it.setForeground(palette.text_on(col))
                t.setItem(r, c, it)
        t.resizeColumnsToContents()
        nodes = [n for n in res.nodes if n.time > 0]
        t = self.tbl_nodes
        heads = ["Узел", "с", "ошибка воздуха", "готов"]
        t.setColumnCount(len(heads))
        t.setHorizontalHeaderLabels(heads)
        t.setRowCount(len(nodes))
        need = res.settings.min_time
        for r, n in enumerate(nodes):
            row = [n.label, "%.1f" % n.time, "%+.1f %%" % ((n.err - 1) * 100),
                   "да" if n.time >= need else "мало"]
            for c, text in enumerate(row):
                it = QtWidgets.QTableWidgetItem(text)
                if c == 3 and n.time < need:
                    it.setForeground(QtGui.QColor("#9b1c1c"))
                t.setItem(r, c, it)
        t.resizeColumnsToContents()

    def set_project(self, project) -> None:
        self.project = project

    def closeEvent(self, ev) -> None:
        if self.session is not None and self.session.state in (
                "connecting", "running"):
            self.stop()
        super().closeEvent(ev)
