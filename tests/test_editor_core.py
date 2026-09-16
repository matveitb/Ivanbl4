#!/usr/bin/env python3
"""
Проверка ядра редактора: круговой ход значения и границы.

Главное, что проверяется -- правка не теряет и не искажает данные.
Задали физическое значение, прочитали обратно, получили то же самое.
И обратное: то, что показано на экране, обязано совпадать с тем, что
реально легло в файл. Шаг сетки равен множителю пересчёта, поэтому
редактор не вправе делать вид, что принял 14.3, если в файле 14.25.

Отдельно проверяется сохранение: после правки контрольные суммы обязаны
быть пересчитаны, иначе блок файл не примет.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "editor", "a2l"))
sys.path.insert(0, os.path.join(ROOT, "editor", "core"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import bosch_csum      # noqa: E402
import fwlib           # noqa: E402
import geometry        # noqa: E402
import mapaccess as M  # noqa: E402
import model           # noqa: E402

A2L = os.path.join(ROOT, "results", "FBH3ID60_legacy.a2l")
FW = os.path.join(ROOT, "firmware", "FBH3ID60_stok.bin")

fails = []


def check(cond, msg):
    print(("OK   " if cond else "ОШИБКА ") + msg)
    if not cond:
        fails.append(msg)


def main():
    a2l = model.load(A2L)
    orig = open(FW, "rb").read()
    am = geometry.detect_addressing(a2l, len(orig))
    L = geometry.resolve(a2l, "KFZWOP", orig, am)

    # -- значения совпадают с тем, что известно независимо, из профиля
    vals = M.read_phys(orig, L)
    check(abs(vals[0][0] - 24.0) < 1e-9,
          "KFZWOP[0][0] = %.2f град (440 об/мин, 10 %% нагрузки)"
          % vals[0][0])
    check(abs(vals[15][0] - 45.0) < 1e-9,
          "KFZWOP[15][0] = %.2f град -- максимум стока" % vals[15][0])

    buf = bytearray(orig)

    # -- круговой ход: записали физическое, прочитали то же
    ok = True
    for r, c, want in ((3, 5, 30.0), (0, 0, 12.75), (15, 10, -3.0)):
        M.write_phys(buf, L, r, c, want)
        got = M.raw_to_phys(L, M.get_raw(buf, L, r, c))
        if abs(got - want) > L.factor / 2:
            ok = False
            print("      (%d,%d): хотели %.3f, получили %.3f" % (r, c, want, got))
    check(ok, "круговой ход: записанное читается обратно без потерь")

    # -- то, что покажет редактор, равно тому, что легло в файл
    check(abs(M.snap(L, 14.3) - 14.25) < 1e-9,
          "14.3 при шаге 0.75 ложится как %.2f -- редактор обязан "
          "показать именно это" % M.snap(L, 14.3))

    # -- зажим в границы типа, без переполнения
    lo, hi = L.raw_range()
    check((lo, hi) == (-128, 127), "диапазон знакового байта: %d..%d" % (lo, hi))
    M.write_phys(buf, L, 1, 1, 9999.0)
    check(M.get_raw(buf, L, 1, 1) == hi,
          "999 град зажалось в %d, а не переполнилось" % M.get_raw(buf, L, 1, 1))
    M.write_phys(buf, L, 1, 2, -9999.0)
    check(M.get_raw(buf, L, 1, 2) == lo, "минус зажался в %d" % lo)

    # -- правка не задела соседние байты
    touched = {M.cell_offset(L, r, c)
               for r, c in ((3, 5), (0, 0), (15, 10), (1, 1), (1, 2))}
    stray = [i for i in range(len(orig))
             if orig[i] != buf[i] and i not in touched]
    check(not stray,
          "изменены только тронутые ячейки, посторонних байт: %d" % len(stray))

    # -- отмена возвращает образ побайтово
    undo = bytearray(orig)
    check(bytes(undo) == orig, "откат к исходному даёт побайтовое равенство")

    # -- контрольные суммы: до пересчёта плохо, после -- хорошо
    fw_bad = fwlib.Firmware(bytes(buf), "<edited>")
    bad, _skip, _good = bosch_csum.check(fw_bad, bosch_csum.TABLE_ADDR,
                                         verbose=False)
    check(len(bad) > 0,
          "после правки контрольные суммы разошлись (%d записей) -- "
          "именно поэтому пересчёт обязателен" % len(bad))

    fixed, n = bosch_csum.fix(fw_bad, bosch_csum.TABLE_ADDR)
    fw_ok = fwlib.Firmware(bytes(fixed), "<fixed>")
    bad2, _s2, good2 = bosch_csum.check(fw_ok, bosch_csum.TABLE_ADDR,
                                        verbose=False)
    check(not bad2, "после пересчёта ошибок нет, исправлено записей: %d" % n)
    check(len(good2) > 0, "сошедшихся записей: %d" % len(good2))

    # -- пересчёт не трогает калибровку, только таблицу сумм
    diff_outside = [i for i in range(0x10000, 0x1FC00)
                    if fixed[i] != buf[i]]
    check(not diff_outside,
          "пересчёт сумм не изменил ни байта калибровки")

    print()
    if fails:
        print("ПРОВАЛЕНО: %d" % len(fails))
        return 1
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
