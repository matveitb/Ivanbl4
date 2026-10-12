#!/usr/bin/env python3
"""
Окно «Смесь по УДК» без экрана: от меню до правки в прошивке.

Проверяется путь человека целиком: меню редактора открывает окно, логи
читаются, каналы распознаются, сдвиг подбирается сам, расчёт наполняет
таблицы, «Записать» кладёт правку в открытую прошивку через историю --
и Ctrl+Z в редакторе её отменяет.

Без PySide6 проверка не считается провалом, как и test_editor_ui.py.
"""

import os
import shutil
import sys
import tempfile

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
    import udk
    import udksim

    tmp = tempfile.mkdtemp(prefix="udkui")
    work = os.path.join(tmp, "work.bin")
    shutil.copy(FW, work)
    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.Format.IniFormat)
    QtCore.QSettings.setPath(QtCore.QSettings.Format.IniFormat,
                             QtCore.QSettings.Scope.UserScope, tmp)
    app = QtWidgets.QApplication([sys.argv[0]])
    app.setOrganizationName(ORG)
    app.setApplicationName(APP)

    try:
        w = MainWindow(work, A2L)
        check(w.act_udk.isEnabled(), "пункт «Смесь по УДК» доступен")

        fm = udk.FuelModel.from_project(w.project)
        tr = udksim.run(fm, fm.hfm_raw)
        elm = os.path.join(tmp, "elm.csv")
        wbp = os.path.join(tmp, "udk.csv")
        with open(elm, "w", encoding="utf-8") as fh:
            fh.write(udksim.ecu_log_carscanner(tr))
        with open(wbp, "w", encoding="cp1251") as fh:
            fh.write(udksim.wb_log(tr, offset=7.5))

        w.open_udk()
        d = w.udk
        check(d is not None and d.isVisible(), "окно открылось")
        check(d.load_ecu(elm) and d.load_wb(wbp), "оба лога прочитаны")
        check(d.cb_role["lam"].currentData() == ("wb", "Лямбда")
              and d.cb_role["tps"].currentData()
              == ("ecu", "Throttle position"),
              "каналы распознаны и выставлены в списках")
        check(abs(d.sp_offset.value() - 7.5) < 0.25,
              "сдвиг подобран сам при открытии лога УДК: %.2f"
              % d.sp_offset.value())

        check(d.rb_hfm.isChecked() and d.chk_keep.isChecked(),
              "по умолчанию: править ДМРВ, цель -- как в прошивке")
        d.rb_kl.setChecked(True)
        d.chk_keep.setChecked(False)
        check(d.sp_want.isEnabled(), "сняли галку -- можно задать свою цель")
        d.sp_want.setValue(0.88)
        d.compute()
        check(d.result is not None and d.tbl_nodes.rowCount() == 6,
              "KLLAMFA: шесть узлов в таблице")
        check(d.tbl_bands.rowCount() >= 5,
              "полный газ по оборотам: %d полос" % d.tbl_bands.rowCount())
        check("KLLAMFA" in d.txt.toPlainText(), "отчёт текстом наполнен")
        check(d.btn_apply.isEnabled(), "есть что записать")

        before = bytes(w.project.buf)
        d.apply()
        check(w.project.changed_maps() == {"KLLAMFA_0", "KLLAMFA_1"},
              "записано в KLLAMFA_0 и _1")
        check(w.windowTitle().startswith("*"),
              "редактор видит несохранённую правку")
        check(not d.btn_apply.isEnabled(),
              "повторно записать тот же расчёт нельзя")
        w.undo()
        w.undo()
        check(bytes(w.project.buf) == before,
              "Ctrl+Z в редакторе отменил правку окна побайтово")

        d.rb_hfm.setChecked(True)
        check(not d.btn_apply.isEnabled(),
              "смена цели сбрасывает старый расчёт")
        d.compute()
        check(d.result is not None and d.result.target == "hfm"
              and d.tbl_nodes.rowCount() > 6,
              "тарировка ДМРВ: узлов в таблице %d" % d.tbl_nodes.rowCount())
        d.apply()
        check(w.project.changed_maps() == {"HFM_LIN"},
              "записано в HFM_LIN")
        w.save()
        import saving
        bad, total = saving.verify(open(work, "rb").read(),
                                   w.project.csum_table)
        check(bad == 0 and total > 0,
              "сохранено из редактора с суммами: неверных %d" % bad)

        d.drop_wb()
        check(d.wb is None and d.cb_role["lam"].currentData() is None,
              "лог УДК убран -- лямбды не стало, расчёт не врёт молча")
        d.compute()
        check(d.result is None and "Не посчиталось" in d.summary.text(),
              "без УДК -- понятный отказ: %s" % d.summary.text()[:60])
        d.close()
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
