#!/usr/bin/env python3
"""
Окно «Снять лог» без экрана: имитатор вместо машины, настоящие часы.

Путь человека: меню -> окно -> выбрать адаптер и ШДК -> «Подключить и
писать» -> приборы ожили, таблицы наполняются -> «Стоп» -> «Открыть в
расчёте» -> лог уже в окне «Смесь по УДК», каналы распознаны. И то же
без ШДК: тогда смесь -- по УДК, родному зонду, и коррекциям.

Идёт несколько секунд по настоящим часам: поток подключения, поток ELM
и поток ШДК -- настоящие, проверяется и то, что окно их дожидается и
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
              and u.cb_role["lam"].currentData() == ("ecu", "ШДК лямбда"),
              "лог открыт в «Смесь по УДК», лямбда ШДК найдена в нём же")

        # без ШДК: смесь по УДК -- напряжение зонда и STFT из PID 14
        d.cb_wb.setCurrentIndex(d.cb_wb.findData("none"))
        path2 = os.path.join(tmp, "без_шдк.csv")
        d.ed_path.setText(path2)
        d.start()
        pump(9.0)
        check(d.gauges["o2v"].text() not in ("–", "")
              and d.gauges["stft"].text() not in ("–", "")
              and d.gauges["lam"].text() == "–",
              "без ШДК приборы УДК и STFT живые: %s В, %s %%"
              % (d.gauges["o2v"].text(), d.gauges["stft"].text()))
        check("УДК, узкий зонд" in d.lb_rates.text(),
              "частота опроса УДК видна: %s" % d.lb_rates.text()[:90])
        d.stop()
        d.btn_calc.click()
        u = w.udk
        check(u.ed_ecu.text() == path2
              and u.cb_role["lam"].currentData() is None
              and u.cb_role["o2v"].currentData()
              == ("ecu", "O2 sensor 1 voltage")
              and u.cb_role["stft"].currentData() is not None,
              "лог без ШДК в расчёте: УДК и коррекции, лямбды нет")

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

        # Bluetooth: порты подписаны по-человечески, «Найти адаптер»
        # выбирает нужный, входящий порт отвергается с объяснением
        import elm as E
        import logger_window as LW
        bt = r"BTHENUM\{00001101-0000-1000-8000-00805F9B34FB}_LOCALMFG&"
        desc = "Стандартный последовательный порт по соединению Bluetooth (%s)"
        fake = [E.classify_port("COM6", desc % "COM6",
                                bt + r"0000\7&1&0&000000000000_00000000",
                                lambda m: ""),
                E.classify_port("COM5", desc % "COM5",
                                bt + r"0002\7&1&0&001DA5686E0D_C00000000",
                                lambda m: "OBDII")]
        real = (E.port_infos, E.find_elm)

        def fake_find(ports, log=None, **kw):
            log("пробую COM5...")
            return "COM5"
        E.port_infos = lambda: sorted(fake, key=lambda p: p.rank)
        E.find_elm = fake_find
        try:
            d.refresh_ports()
            # выбранным остаётся прошлый выбор (имитатор), если он есть
            check(d.cb_elm.itemText(0) == "COM5 · Bluetooth «OBDII» · исходящий"
                  and d.cb_elm.itemData(0) == "COM5",
                  "в списке адаптер подписан именем и «исходящий», он первым: "
                  "%s" % d.cb_elm.itemText(0))
            d.cb_elm.setCurrentIndex(d.cb_elm.findData("COM6"))
            d.find_adapter()
            pump(1.0)
            check(d.cb_elm.currentData() == "COM5"
                  and "нашёл: COM5" in d.messages.toPlainText(),
                  "«Найти адаптер» выбрал COM5 сам")
            d.cb_elm.setCurrentIndex(d.cb_elm.findData("COM6"))
            d.cb_wb.setCurrentIndex(d.cb_wb.findData("none"))
            d.start()
            pump(1.5)
            last = d.messages.toPlainText().splitlines()[-1]
            check(d.session.state == "failed" and "входящий порт" in last,
                  "входящий порт Bluetooth -- понятный отказ: %s" % last[9:90])
        finally:
            E.port_infos, E.find_elm = real
            check(LW.E is E, "окно пользуется тем же модулем elm")
            d.refresh_ports()

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
