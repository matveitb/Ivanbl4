#!/usr/bin/env python3
"""
Проверка окна без экрана.

Qt умеет работать на платформе offscreen -- окно создаётся, виджеты
наполняются, сигналы ходят, просто ничего не рисуется на дисплее. Этого
достаточно, чтобы проверить главное: дерево наполнилось, таблица совпала
с тем, что показывает командная строка, кнопка правки действительно
меняет буфер, а отмена возвращает его побайтово.

Если PySide6 не установлен, проверка не считается провалом: ядро
редактора от Qt не зависит, и остальные проверки идут своим ходом.
Ставится он одной строкой: pip install -r requirements-editor.txt
"""

import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "editor", "a2l"))
sys.path.insert(0, os.path.join(ROOT, "editor", "core"))
sys.path.insert(0, os.path.join(ROOT, "editor", "ui"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

A2L = os.path.join(ROOT, "results", "FBH3ID60_legacy.a2l")
FW = os.path.join(ROOT, "firmware", "FBH3ID60_stok.bin")

fails = []


def check(cond, msg):
    print(("OK   " if cond else "ОШИБКА ") + msg)
    if not cond:
        fails.append(msg)


def main():
    try:
        from PySide6 import QtWidgets
    except ImportError as exc:
        print("ПРОПУЩЕНО: PySide6 не установлен (%s)" % exc)
        print("  поставить: pip install -r requirements-editor.txt")
        return 0

    from PySide6 import QtCore
    from main_window import MainWindow, ORG, APP

    tmp = tempfile.mkdtemp(prefix="ktpui")
    work = os.path.join(tmp, "work.bin")
    shutil.copy(FW, work)

    # Настройки окна (недавние файлы, размеры) уводим во временную папку:
    # проверка не должна лезть в настоящие настройки пользователя и тем
    # более менять его список недавних файлов.
    QtCore.QSettings.setDefaultFormat(QtCore.QSettings.Format.IniFormat)
    QtCore.QSettings.setPath(QtCore.QSettings.Format.IniFormat,
                             QtCore.QSettings.Scope.UserScope, tmp)

    app = QtWidgets.QApplication([sys.argv[0]])
    app.setOrganizationName(ORG)
    app.setApplicationName(APP)

    w = MainWindow(work, A2L)
    check(w.project is not None and len(w.project.layouts) == 858,
          "окно открыло прошивку и описание: карт %d"
          % len(w.project.layouts))
    check(w.tree.tree.topLevelItemCount() == 24,
          "в дереве групп: %d" % w.tree.tree.topLevelItemCount())

    # -- таблица совпадает с ядром ---------------------------------------
    import mapaccess as M
    w.open_map("KFZWOP")
    g = w.grid
    L = w.project.layout("KFZWOP")
    check((g.rowCount(), g.columnCount()) == (L.ny, L.nx),
          "размер таблицы %dx%d совпал с раскладкой"
          % (g.rowCount(), g.columnCount()))
    vals = M.read_phys(w.project.buf, L)
    same = all(abs(float(g.item(r, c).text()) - vals[r][c]) < 5e-3
               for r in range(L.ny) for c in range(L.nx))
    check(same, "каждая ячейка на экране совпала со значением в буфере")

    xs, ys = M.axes(w.project.buf, L)
    check(g.horizontalHeaderItem(0).text() == "%d" % round(xs[0])
          and g.verticalHeaderItem(0).text() == "%d" % round(ys[0]),
          "заголовки взяты с осей: X от %g, Y от %g" % (xs[0], ys[0]))

    # -- поиск переключает дерево в плоский список ------------------------
    w.tree.search.setText("KFZW")
    top = w.tree.tree.topLevelItem(0)
    check(w.tree.tree.topLevelItemCount() == 1 and top.childCount() > 1,
          "поиск дал плоский список из %d карт" % top.childCount())
    w.tree.search.clear()
    check(w.tree.tree.topLevelItemCount() == 24, "после очистки дерево вернулось")

    # -- кнопка правки меняет буфер и отменяется --------------------------
    snapshot = bytes(w.project.buf)
    g.clearSelection()
    for r in range(3, 6):
        for c in range(2, 5):
            g.item(r, c).setSelected(True)
    w.value.setValue(1.5)
    w.run_op("+")
    check("ячеек изменено 9" in w.statusBar().currentMessage(),
          "кнопка «+» отчиталась: %s" % w.statusBar().currentMessage())
    check(bytes(w.project.buf) != snapshot, "буфер действительно изменился")
    check(w.windowTitle().startswith("* "),
          "в заголовке окна появилась звёздочка: %s" % w.windowTitle())

    shown = float(g.item(3, 2).text())
    check(abs(shown - M.read_phys(w.project.buf, L)[3][2]) < 5e-3,
          "после правки на экране то, что легло в буфер: %g" % shown)

    w.undo()
    check(bytes(w.project.buf) == snapshot,
          "отмена из окна вернула образ побайтово")
    check(not w.windowTitle().startswith("* "), "звёздочка ушла")

    # -- ввод с клавиатуры идёт через историю -----------------------------
    g.item(0, 0).setText("14.3")
    check(g.item(0, 0).text() == "14.25",
          "набранное 14.3 показано как легло: %s" % g.item(0, 0).text())
    check(w.project.history.can_undo, "набранное с клавиатуры попало в историю")
    w.undo()
    check(bytes(w.project.buf) == snapshot, "и тоже отменилось начисто")

    # -- нечисловой ввод не портит ячейку ---------------------------------
    was = g.item(1, 1).text()
    g.item(1, 1).setText("ерунда")
    check(g.item(1, 1).text() == was,
          "нечисловой ввод отброшен, в ячейке осталось %s" % was)
    check(bytes(w.project.buf) == snapshot, "и буфер не тронут")

    # -- карта только для чтения не правится -------------------------------
    ro = next((n for n, lay in w.project.layouts.items() if not lay.editable),
              "")
    if ro:
        w.tree.select_map(ro)
        w.open_map(ro)
        w.run_op("+")
        check(bytes(w.project.buf) == snapshot,
              "карта «%s» только для чтения -- правка отклонена: %s"
              % (ro, w.statusBar().currentMessage()))
    else:
        print("     (в этом A2L карт только для чтения нет -- "
              "проверять нечего)")

    # -- графики ----------------------------------------------------------
    w.tree.select_map("KFZWOP")
    w.open_map("KFZWOP")
    check(w.curve.lay is not None and w.surface.lay is not None,
          "обе рисовалки получили карту")

    w.grid.setCurrentCell(7, 3)
    check(w.curve.row == 7, "кривая следует за строкой таблицы: %d"
          % w.curve.row)

    def shot(widget):
        return widget.grab().toImage()

    before_c, before_s = shot(w.curve), shot(w.surface)
    g.clearSelection()
    for c in range(g.columnCount()):
        g.item(7, c).setSelected(True)
    w.value.setValue(6.0)
    w.run_op("+")
    check(shot(w.curve) != before_c, "кривая перерисовалась после правки")
    check(shot(w.surface) != before_s, "поверхность перерисовалась после правки")
    w.undo()
    check(shot(w.curve) == before_c, "и вернулась после отмены")
    check(bytes(w.project.buf) == snapshot, "буфер тоже вернулся")

    # поворот меняет картинку, двойной щелчок возвращает исходный вид
    was = shot(w.surface)
    w.surface.yaw += 0.7
    w.surface.update()
    check(shot(w.surface) != was, "поворот поверхности виден")
    w.surface.mouseDoubleClickEvent(None)
    check(shot(w.surface) == was, "двойной щелчок вернул исходный вид")

    # -- сравнение прошивок -------------------------------------------------
    other = os.path.join(ROOT, "firmware", "FBH3ID60 e2 tun csok v2___.bin")
    if os.path.exists(other):
        w.diff.compare_with(other)
        names = w.diff.changed_names()
        check({"KFZW", "KFZW2", "KFZWOP", "KFLBTS"} <= names,
              "в списке изменённых знакомые карты: изменилось %d карт"
              % len(names))
        check(w.diff.table.rowCount() == len(w.diff.result.maps),
              "список заполнен: строк %d" % w.diff.table.rowCount())
        check("вне известных карт" in w.diff.head.text(),
              "про изменения вне размеченного сказано прямо, а не умолчано")

        w.open_map("KFZW")
        LK = w.project.layout("KFZW")
        marked = w.grid.diff_cells
        check(len(marked) == next(d.changed for d in w.diff.result.maps
                                  if d.name == "KFZW"),
              "обведённых ячеек столько же, сколько изменённых: %d"
              % len(marked))

        # показ чужих чисел и разницы -- и запрет правки в этих режимах
        mine = float(w.grid.item(8, 5).text())
        w.diff.mode.setCurrentIndex(1)                       # значения второй
        theirs = float(w.grid.item(8, 5).text())
        w.diff.mode.setCurrentIndex(2)                       # разница
        delta = float(w.grid.item(8, 5).text())
        check(abs((theirs - mine) - delta) < 5e-3,
              "разница сходится: %g - %g = %g" % (theirs, mine, delta))

        guard = bytes(w.project.buf)
        w.value.setValue(5.0)
        w.grid.clearSelection()
        w.grid.item(8, 5).setSelected(True)
        w.run_op("+")
        check(bytes(w.project.buf) == guard,
              "правка при показе чужих чисел отклонена: %s"
              % w.statusBar().currentMessage())

        w.diff.mode.setCurrentIndex(0)                       # обратно свои
        check(abs(float(w.grid.item(8, 5).text()) - mine) < 5e-3,
              "вернулись к своим числам: %g" % mine)
        check(w.curve.other, "кривая знает про вторую прошивку")

        w.diff.clear()
        check(not w.grid.diff_cells and not w.curve.other,
              "закрытие сравнения убрало и обводку, и пунктир")
    else:
        print("     (второй прошивки нет на месте -- сравнение не проверено)")

    # -- свои группы и режим дерева ----------------------------------------
    w.tree.tree.clearSelection()
    w.tree.select_map("KFZWOP")
    check(w.tree.picked_maps() == ["KFZWOP"],
          "щелчок по карте даёт её саму")
    w.tree._add("Работа", ["KFZWOP", "KFZW"])
    check(w.project.user_groups["Работа"] == ["KFZWOP", "KFZW"],
          "карты легли в свою группу")
    check(os.path.exists(w.project.project_path),
          "файл проекта записан сразу: %s"
          % os.path.basename(w.project.project_path))
    check(w.tree.tree.topLevelItemCount() == 24,
          "пока выбрана группировка из описания, дерево не поменялось")

    w._set_group_mode("user")
    titles = [w.tree.tree.topLevelItem(i).text(0)
              for i in range(w.tree.tree.topLevelItemCount())]
    check(titles == ["Работа", "Вне своих групп"],
          "в своём режиме дерево из своих групп: %s" % titles)
    top = w.tree.tree.topLevelItem(0)
    w.tree.tree.clearSelection()
    top.setSelected(True)
    check(w.tree.picked_maps() == ["KFZWOP", "KFZW"],
          "щелчок по заголовку берёт всю группу")
    w._set_group_mode("auto")
    check(w.tree.tree.topLevelItemCount() == 24, "режим вернулся")

    # -- недавние файлы ----------------------------------------------------
    rec = w._recent()
    check(rec and rec[0] == (A2L, work),
          "последняя открытая пара запомнена первой: %s"
          % (os.path.basename(rec[0][1]) if rec else "пусто"))
    check(w.menu_recent.actions() and w.menu_recent.actions()[0].isEnabled(),
          "в меню «Недавние» есть рабочая строка")

    # ни одна карта не роняет рисовалки
    bad = []
    for name in w.project.layouts:
        for widget in (w.curve, w.surface):
            try:
                widget.set_map(w.project, name)
                widget.grab()
            except Exception as exc:                        # noqa: BLE001
                bad.append("%s: %s" % (name, exc))
    check(not bad,
          "все %d карт прошли через кривую и поверхность%s"
          % (len(w.project.layouts),
             "" if not bad else ", кроме: " + "; ".join(bad[:3])))

    shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print("ПРОВАЛЕНО: %d" % len(fails))
        return 1
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
