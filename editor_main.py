#!/usr/bin/env python3
"""
Точка входа для сборки .exe.

PyInstaller собирает ОДИН скрипт, а не пакет, и запускать `python -m
editor` внутри сборки нечему. Поэтому здесь отдельный вход, который
делает ровно то же самое. Для обычной работы он не нужен -- годится и
`python3 -m editor`.

Отдельно -- ключ --selftest. Он нужен потому, что «собралось» и
«запустилось» не значит «работает»: половина импортов в этом проекте
ленивая. torque.interp тянется только при нажатии «протянуть»,
bosch_csum -- только при сохранении. Собранный файл спокойно откроется и
упадёт через полчаса работы, когда человек нажмёт кнопку, которую при
проверке никто не нажал.

Поэтому самопроверка живёт ВНУТРИ собранного файла и прогоняется на той
же машине, где он собран, до того как попадёт к человеку.
"""

import os
import sys
import tempfile

from editor.app import main


def selftest() -> int:
    """Прогнать внутри сборки всё, что делается руками. Печатает по-русски."""
    from editor import paths
    import shutil

    fails = []
    # В оконной сборке под Windows stdout нет вовсе -- print() там валится
    # с AttributeError на None. Печать поэтому защищена, а настоящий ответ
    # даёт код возврата: он доходит до сборочной машины при любом раскладе.
    def say(msg):
        try:
            print(msg)
        except Exception:                                   # noqa: BLE001
            pass

    def check(cond, msg):
        say(("OK   " if cond else "ОШИБКА ") + msg)
        if not cond:
            fails.append(msg)

    a2l_path = paths.bundled("results", "FBH3ID60_legacy.a2l")
    check(os.path.exists(a2l_path), "описание внутри сборки: %s" % a2l_path)
    if not os.path.exists(a2l_path):
        return 1

    fw = None
    for cand in (sys.argv[2] if len(sys.argv) > 2 else "",
                 paths.bundled("firmware", "FBH3ID60_stok.bin")):
        if cand and os.path.exists(cand):
            fw = cand
            break
    if not fw:
        say("ПРОПУЩЕНО: прошивка не передана и в сборку не вложена")
        say("  запуск: РедакторКалибровок --selftest путь\\к\\прошивке.bin")
        return 0 if not fails else 1

    tmp = tempfile.mkdtemp(prefix="selftest")
    work = os.path.join(tmp, "work.bin")
    shutil.copy(fw, work)

    import project as proj          # ядро
    import mapaccess as M
    import edits
    import saving

    p = proj.Project.open(a2l_path, work)
    check(len(p.layouts) > 700, "описание разобрано: карт %d" % len(p.layouts))
    check(p.csum_table >= 0,
          "таблица сумм найдена: 0x%X" % p.csum_table)

    L = p.layout("KFZW")
    before = bytes(p.buf)
    sel = [(r, c) for r in range(4, 8) for c in range(3, 6)]

    # каждая операция тянет свой ленивый импорт -- потому и перечислены все
    p.apply(edits.op_add(p.buf, L, sel, 1.5))
    check(p.changed_maps() == {"KFZW"}, "правка «+»: изменена KFZW")
    p.apply(edits.op_percent(p.buf, L, sel, 5.0))
    p.apply(edits.op_raw_add(p.buf, L, sel, 1))
    ed = edits.op_interpolate(p.buf, L, [(9, c) for c in range(L.nx)])
    p.apply(ed)
    check(ed.count > 0, "«протянуть» сработала (тянет torque): %d ячеек"
          % ed.count)
    p.apply(edits.op_smooth(p.buf, L, sel))

    n = 0
    while p.history.can_undo:
        p.undo()
        n += 1
    check(n == 5 and bytes(p.buf) == before,
          "отмена %d операций вернула образ побайтово" % n)
    while p.history.can_redo:
        p.redo()

    res = p.save()
    check(res.fixed >= 0 and res.table >= 0,
          "сохранение: сумм исправлено %d из %d" % (res.fixed, res.records))
    bad, total = saving.verify(open(work, "rb").read(), p.csum_table)
    check(bad == 0, "в сохранённом файле неверных сумм нет (записей %d)"
          % total)

    # сравнение прошивок -- тянет fwdiff
    import compare as C
    r = C.compare(p.layouts, open(fw, "rb").read(), open(work, "rb").read())
    check(any(d.name == "KFZW" for d in r.maps),
          "сравнение видит правку: изменённых карт %d" % len(r.maps))

    # оси и таблица -- тянут geometry и mapaccess
    xs, ys = M.axes(p.buf, p.layout("KFMIOP"))
    check(round(ys[0]) == 440 and round(xs[0]) == 10,
          "оси KFMIOP на месте: обороты с %g, нагрузка с %g" % (ys[0], xs[0]))

    shutil.rmtree(tmp, ignore_errors=True)
    say("")
    if fails:
        say("САМОПРОВЕРКА ПРОВАЛЕНА: %d" % len(fails))
        return 1
    say("САМОПРОВЕРКА ПРОЙДЕНА")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(main())
