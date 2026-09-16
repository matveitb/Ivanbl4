#!/usr/bin/env python3
"""
Проверка слоя правок: операции, история, сохранение, дерево.

Что именно проверяется и почему.

ОТМЕНА ОБЯЗАНА ВОЗВРАЩАТЬ БАЙТЫ. Не «примерно те же значения», а тот же
образ побайтово. Поэтому история хранит сырые значения, а не физические:
физические при записи округляются к шагу сетки, и обратный пересчёт
исходные байты не вернул бы.

ЧТО ПОКАЗАНО -- ТО И ЛЕЖИТ. Введённое значение прилипает к сетке
множителя и зажимается в диапазон типа. Проверяем оба случая явно.

ПРОТЯЖКА ИДЁТ ПО ОСИ, А НЕ ПО НОМЕРАМ ЯЧЕЕК. Оси у нас неравномерные,
и протяжка по индексу дала бы излом. Проверяем, что наклон между
крайними точками держится с точностью до одного шага сетки.

ПРАВКА НЕ ЗАДЕВАЕТ СОСЕДЕЙ. После сохранения расходиться с оригиналом
имеют право только байты самой карты и таблица сумм -- больше ничто.
"""

import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "editor", "a2l"))
sys.path.insert(0, os.path.join(ROOT, "editor", "core"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import edits           # noqa: E402
import mapaccess as M  # noqa: E402
import project as proj  # noqa: E402
import saving          # noqa: E402

A2L = os.path.join(ROOT, "results", "FBH3ID60_legacy.a2l")
FW = os.path.join(ROOT, "firmware", "FBH3ID60_stok.bin")

fails = []


def check(cond, msg):
    print(("OK   " if cond else "ОШИБКА ") + msg)
    if not cond:
        fails.append(msg)


def main():
    tmp = tempfile.mkdtemp(prefix="ktp")
    work = os.path.join(tmp, "work.bin")
    shutil.copy(FW, work)

    p = proj.Project.open(A2L, work)
    # Точное число не закрепляем: оно меняется, когда генератор выбрасывает
    # карты, лезущие на чужие байты. Закрепляем то, что должно держаться
    # всегда -- описание разобралось целиком и непусто.
    check(len(p.layouts) == len(p.a2l.characteristics) > 500,
          "карт разобрано: %d" % len(p.layouts))
    check(p.csum_table == 0x1FC00,
          "таблица сумм найдена сама: 0x%X" % p.csum_table)

    # -- дерево ----------------------------------------------------------
    t = p.tree()
    check(t.count == len(p.layouts),
          "в дереве все карты: %d в %d группах" % (t.count, len(t.children)))
    titles = [c.title for c in t.children]
    check("Моментная модель" in titles and "Цикловое наполнение" in titles,
          "группы названы по-русски, среди них: %s" % ", ".join(titles[:3]))
    dup = t.count - len({n for c in t.children for n in c.maps})
    check(dup == 0, "ни одна карта не попала в две группы")

    # -- поиск -----------------------------------------------------------
    found = p.search("kfzwop")
    check("KFZWOP" in found and all("kfzwop" in (n + p.desc(n)).lower()
                                    for n in found),
          "поиск ищет и по имени, и по описанию: %s" % ", ".join(found))
    check(len(p.search("")) == len(p.layouts), "пустой запрос -- все карты")

    L = p.layout("KFZWOP")
    snapshot = bytes(p.buf)

    # -- прилипание к сетке и зажим в тип --------------------------------
    p.apply(edits.op_set(p.buf, L, [(0, 0)], 14.3))
    got = M.read_phys(p.buf, L)[0][0]
    check(abs(got - 14.25) < 1e-9,
          "14.3 при шаге 0.75 легло как %g, а не как введено" % got)

    p.apply(edits.op_set(p.buf, L, [(0, 1)], 999.0))
    got = M.read_phys(p.buf, L)[0][1]
    check(abs(got - 127 * 0.75) < 1e-9,
          "999 зажалось в потолок знакового байта: %g" % got)

    p.apply(edits.op_set(p.buf, L, [(0, 2)], -999.0))
    got = M.read_phys(p.buf, L)[0][2]
    check(abs(got - (-128 * 0.75)) < 1e-9,
          "-999 зажалось в пол знакового байта: %g" % got)

    # -- прибавление и проценты ------------------------------------------
    was = M.read_phys(p.buf, L)[5][3]
    p.apply(edits.op_add(p.buf, L, [(5, 3)], 2.25))
    check(abs(M.read_phys(p.buf, L)[5][3] - (was + 2.25)) < 1e-9,
          "+2.25 дало %g из %g" % (M.read_phys(p.buf, L)[5][3], was))

    was = M.read_phys(p.buf, L)[6][3]
    p.apply(edits.op_percent(p.buf, L, [(6, 3)], 10.0))
    now = M.read_phys(p.buf, L)[6][3]
    check(abs(now - was * 1.1) <= L.factor / 2 + 1e-9,
          "+10%% дало %g из %g (в пределах половины шага)" % (now, was))

    # -- сырой шаг --------------------------------------------------------
    before = M.get_raw(p.buf, L, 7, 3)
    p.apply(edits.op_raw_add(p.buf, L, [(7, 3)], 1))
    check(M.get_raw(p.buf, L, 7, 3) == before + 1,
          "сырое +1 сдвинуло ровно на шаг сетки")

    # -- протяжка по оси --------------------------------------------------
    row = 9
    sel = [(row, c) for c in range(L.nx)]
    p.apply(edits.op_interpolate(p.buf, L, sel))
    xs, _ = M.axes(p.buf, L)
    vals = M.read_phys(p.buf, L)[row]
    k = (vals[-1] - vals[0]) / (xs[-1] - xs[0])
    worst = max(abs(vals[i] - (vals[0] + k * (xs[i] - xs[0])))
                for i in range(L.nx))
    check(worst <= L.factor / 2 + 1e-9,
          "протяжка легла на прямую по оси, худшее отклонение %.3f "
          "при половине шага %.3f" % (worst, L.factor / 2))

    # -- сглаживание не выходит за выделение ------------------------------
    sel = [(r, c) for r in range(2, 5) for c in range(2, 5)]
    outside = bytes(p.buf)
    ed = edits.op_smooth(p.buf, L, sel)
    p.apply(ed)
    touched = {(r, c) for r, c, _o, _n in ed.cells}
    check(touched <= set(sel),
          "сглаживание тронуло только выделенные ячейки (%d)" % len(touched))
    changed = [i for i in range(len(p.buf)) if p.buf[i] != outside[i]]
    inside = all(L.data_off <= i < L.end for i in changed)
    check(inside, "сглаживание не вышло за пределы карты")

    # -- отмена возвращает байты -----------------------------------------
    n = 0
    while p.history.can_undo:
        p.undo()
        n += 1
    check(n == 8, "отменено операций: %d" % n)
    check(bytes(p.buf) == snapshot,
          "после полной отмены образ побайтово равен исходному")

    # -- повтор возвращает правки ----------------------------------------
    m = 0
    while p.history.can_redo:
        p.redo()
        m += 1
    check(m == n, "повторено столько же: %d" % m)
    check(p.changed_maps() == {"KFZWOP"},
          "изменённой числится ровно одна карта: %s" % p.changed_maps())

    # -- сохранение -------------------------------------------------------
    check(p.dirty, "до сохранения проект помечен как изменённый")
    res = p.save()
    check(not p.dirty, "после сохранения пометка снята")
    check(res.backup.endswith(".bak") and os.path.exists(res.backup),
          "резервная копия оригинала сделана: %s"
          % os.path.basename(res.backup))

    saved = open(work, "rb").read()
    orig = open(FW, "rb").read()
    stray = [i for i in range(len(orig))
             if saved[i] != orig[i]
             and not (L.data_off <= i < L.end)
             and not (0x1FC00 <= i < 0x20000)]
    check(not stray,
          "вне карты и вне таблицы сумм не изменилось ни байта")

    bad, total = saving.verify(saved, p.csum_table)
    check(bad == 0, "в сохранённом файле неверных сумм нет (записей %d)" % total)
    check(res.fixed >= 1, "сумм пересчитано: %d" % res.fixed)

    # -- сохранение без пересчёта портит суммы, и это видно ---------------
    plain = os.path.join(tmp, "nocsum.bin")
    saving.save(p.buf, plain, fix_checksums=False, backup=False)
    bad2, _ = saving.verify(open(plain, "rb").read(), p.csum_table)
    check(bad2 > 0,
          "без пересчёта суммы действительно расходятся (%d записей) -- "
          "значит переключатель не декоративный" % bad2)

    # -- свои группы и файл проекта ----------------------------------------
    p.add_to_group("Мои карты", ["KFZWOP", "KFZW"])
    p.add_to_group("Ещё", ["KFZW", "KFZW2"])
    check(p.user_groups["Мои карты"] == ["KFZWOP"],
          "карта живёт ровно в одной своей группе: %s"
          % p.user_groups["Мои карты"])

    # пока выбрана группировка из описания, своя ничего не прячет
    kids = [c.title for c in p.tree().children]
    check("Моментная модель" in kids and "Мои карты" not in kids,
          "своя группировка не перебивает описание сама собой")

    p.group_mode = "user"
    kids = [c.title for c in p.tree().children]
    check(kids == ["Мои карты", "Ещё", "Вне своих групп"],
          "в своём режиме дерево из своих групп: %s" % kids)
    check(p.tree().count == len(p.layouts),
          "и в нём по-прежнему все карты: %d" % p.tree().count)

    p.save_project_file()
    p2 = proj.Project.open(A2L, work)
    check(p2.user_groups.get("Ещё") == ["KFZW", "KFZW2"]
          and p2.group_mode == "user",
          "своя группировка и режим пережили перезапуск")

    p2.remove_from_group("Ещё", ["KFZW"])
    p2.drop_group("Мои карты")
    p2.rename_group("Ещё", "Работа")
    check(list(p2.user_groups) == ["Работа"]
          and p2.user_groups["Работа"] == ["KFZW2"],
          "убрать, удалить и переименовать работают: %s" % p2.user_groups)

    shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print("ПРОВАЛЕНО: %d" % len(fails))
        return 1
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
