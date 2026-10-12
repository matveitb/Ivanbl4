#!/usr/bin/env python3
"""
Окно «Снять лог» без экрана: имитатор вместо машины, настоящие часы.

Путь человека: меню -> окно -> выбрать адаптер и УДК -> «Подключить и
писать» -> приборы ожили, таблицы наполняются -> «Стоп» -> «Открыть в
расчёте» -> лог уже в окне «Смесь по УДК», каналы распознаны.

Идёт несколько секунд по настоящим часам: поток подключения, поток ELM
и поток УДК -- настоящие, проверяется и то, что окно их дожидается и
останавливает.
"""

import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, ROOT)
for sub in (("editor", "a2l"), ("editor", "core"), ("editor", "ui"),
            ("tools",)):
    sys.path.insert(0, os.path.join(ROOT, *sub))

A2L = os.path.join(ROOT, "results", "FBH3ID60_legacy.a2l")
FW = os.path.join(ROOT, "firmware", "FBH3ID60_v2M.bin")

fails = []


def check(cond, msg):
    print(("OK   " if cond else "ОШИБКА ") + msg)
    if not cond:
        fails.append(msg)


def main():
    try:
        from PySide6 import QtCore, QtWidgets
    except ImportError as exc:
        print("ПРОПУЩЕНО: PySide6 не установлен (%s)" % exc)
        return 0
    from main_window import MainWindow, ORG, APP

    tmp = tempfile.mkdtemp(prefix="logui")
    work = os.path.join(tmp, "work.bin")
    shutil.copy(FW, work)
    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.Format.IniFormat)
    QtCore.QSettings.setPath(QtCore.QSettings.Format.IniFormat,
                             QtCore.QSettings.Scope.UserScope, tmp)
    app = QtWidgets.QApplication([sys.argv[0]])
    app.setOrganizationName(ORG)
    app.setApplicationName(APP)

    def pump(sec):
        end = time.monotonic() + sec
        while time.monotonic() < end:
            app.processEvents()
            time.sleep(0.03)

    try:
        w = MainWindow(work, A2L)
        check(w.act_log.isEnabled(), "пункт «Снять лог» в меню")
        w.open_logger()
        d = w.logger
        check(d is not None and d.isVisible(), "окно логгера открылось")
        check(d.cb_elm.findData("demo") >= 0 and d.cb_elm.findData("wifi") >= 0,
              "в списке адаптеров есть Wi-Fi и имитатор")
        d.cb_elm.setCurrentIndex(d.cb_elm.findData("demo"))
        d.cb_wb.setCurrentIndex(d.cb_wb.findData("demo"))
        path = os.path.join(tmp, "лог.csv")
        d.ed_path.setText(path)
        d.start()
        check(not d.btn_start.isEnabled() and d.btn_stop.isEnabled(),
              "пошло: «Подключить» погасла, «Стоп» горит")
        pump(9.0)
        txt = d.messages.toPlainText()
        check("ELM327" in txt and "пишу в" in txt,
              "подключение видно в сообщениях")
        check(d.gauges["rpm"].text() not in ("–", "")
              and d.gauges["lam"].text() not in ("–", ""),
              "приборы ожили: обороты %s, λ %s" % (
                  d.gauges["rpm"].text(), d.gauges["lam"].text()))
        check(d.gauges["lcmd"].text() == "1.000",
              "цель по прошивке v2M видна: %s" % d.gauges["lcmd"].text())
        check("Гц" in d.lb_rates.text(), "частоты опроса видны")
        d.stop()
        check(d.btn_start.isEnabled() and d.btn_calc.isEnabled(),
              "после «Стоп» можно открыть в расчёте")
        check(d.session.logger is not None and not d.session.logger.running,
              "потоки остановлены")
        check(os.path.getsize(path) > 500, "лог на диске: %d байт"
              % os.path.getsize(path))
        check(d.ed_path.text() != path,
              "следующий лог пойдёт в новый файл, этот не затрётся")
        d.btn_calc.click()
        u = w.udk
        check(u is not None and u.ed_ecu.text() == path
              and u.cb_role["lam"].currentData() == ("ecu", "УДК лямбда"),
              "лог открыт в «Смесь по УДК», лямбда найдена в нём же")

        # «Стоп» посреди подключения: окно не замирает, запись не
        # стартует потом сама
        d.ed_path.setText(os.path.join(tmp, "оборвали.csv"))
        d.start()
        pump(0.3)
        t0 = time.monotonic()
        d.stop()
        check(time.monotonic() - t0 < 2.0,
              "«Стоп» во время подключения не подвесил окно: %.1f с"
              % (time.monotonic() - t0))
        pump(4.0)
        lg = d.session.logger
        check(d.session.state in ("stopped", "failed")
              and (lg is None or not lg.running),
              "после отмены запись не началась: %s" % d.session.state)

        # плохой порт -- понятная ошибка, окно не висит
        d.cb_elm.addItem("нет такого", "COM_NO_SUCH_PORT_99")
        d.cb_elm.setCurrentIndex(d.cb_elm.count() - 1)
        d.cb_wb.setCurrentIndex(d.cb_wb.findData("none"))
        d.start()
        pump(2.0)
        check(d.session.state == "failed" and "ОШИБКА" in
              d.messages.toPlainText().splitlines()[-1]
              and d.btn_start.isEnabled(),
              "несуществующий порт: %s"
              % d.messages.toPlainText().splitlines()[-1][9:80])
        d.close()
        u.close()
        w.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print("ПРОВАЛЕНО: %d" % len(fails))
        return 1
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
