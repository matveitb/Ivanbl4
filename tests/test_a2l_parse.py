#!/usr/bin/env python3
"""
Проверка разбора ASAP2.

Разбор -- слой, ошибка в котором не видна глазом: таблица покажет
соседние байты, и правка испортит другую карту. Поэтому проверяется не
«разобралось без исключения», а конкретные числа, известные независимо.

Отдельно закрепляется ловушка, на которой я уже споткнулся: в блоке
AXIS_PTS между раскладкой и пересчётом стоит поле MaxDiff. Пропустив
его, разбор берёт пересчётом ноль, молча получает единичный множитель и
показывает сырые значения оси вместо оборотов.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "editor", "a2l"))
sys.path.insert(0, os.path.join(ROOT, "editor", "core"))

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
    check(len(a2l.characteristics) > 800,
          "карт разобрано: %d" % len(a2l.characteristics))
    check(len(a2l.axis_pts) >= 6, "осей-объектов: %d" % len(a2l.axis_pts))
    check(len(a2l.compu) > 100, "пересчётов: %d" % len(a2l.compu))
    check(len(a2l.layouts) >= 13, "раскладок: %d" % len(a2l.layouts))

    # -- поле MaxDiff в AXIS_PTS не должно съезжать
    ap = a2l.axis_pts.get("SNM16_OP")
    check(ap is not None and ap.compu == "CM_40_0_rpm",
          "SNM16_OP: пересчёт %s (а не MaxDiff)"
          % (ap.compu if ap else "нет"))
    check(ap is not None and ap.max_points == 16,
          "SNM16_OP: точек %s" % (ap.max_points if ap else "?"))

    # -- пересчёт обращается верно
    cm = a2l.compu_of("CM_0p75_0_gradKW")
    check(cm.linear and abs(cm.factor - 0.75) < 1e-12,
          "CM_0p75_0_gradKW: множитель %g" % cm.factor)

    buf = open(FW, "rb").read()
    am = geometry.detect_addressing(a2l, len(buf))
    check(am.subtract == 0,
          "адресация: у нас в A2L файловые смещения, вычитаем 0x%X"
          % am.subtract)

    # -- голая сетка: адрес указывает прямо на данные
    L = geometry.resolve(a2l, "KFZWOP", buf, am)
    check(L.data_off == 0x10529, "KFZWOP: данные 0x%05X" % L.data_off)
    check((L.nx, L.ny) == (11, 16), "KFZWOP: %dx%d" % (L.nx, L.ny))
    check(L.width == 1 and L.signed, "KFZWOP: знаковый байт")
    check(abs(L.factor - 0.75) < 1e-12, "KFZWOP: множитель %g" % L.factor)

    xs = L.x_axis.values(buf)
    ys = L.y_axis.values(buf)
    check([round(v) for v in ys[:4]] == [440, 680, 800, 920],
          "KFZWOP: ось оборотов начинается с %s"
          % [round(v) for v in ys[:4]])
    check([round(v) for v in xs[:4]] == [10, 15, 20, 25],
          "KFZWOP: ось нагрузки начинается с %s"
          % [round(v) for v in xs[:4]])

    # -- раскладка с осями внутри: адрес указывает на ЗАГОЛОВОК
    E = geometry.resolve(a2l, "KFETAZW", buf, am)
    check(E.header_off == 0x101F4, "KFETAZW: заголовок 0x%05X" % E.header_off)
    check(E.data_off == E.header_off + 2 + E.nx + E.ny,
          "KFETAZW: данные 0x%05X = заголовок + 2 + %d + %d"
          % (E.data_off, E.nx, E.ny))

    # -- заголовочная карта не должна читаться перевёрнутой
    #
    # Первой в блоке лежит ось СТРОК, а не столбцов: подряд в файле идут
    # значения второй оси. Проверяем на KFMIRL, где ошибка видна сразу --
    # 16 на 12, а не 12 на 16, и обороты обязаны быть по строкам.
    R = geometry.resolve(a2l, "KFMIRL", buf, am)
    check((R.nx, R.ny) == (12, 16), "KFMIRL: %dx%d" % (R.nx, R.ny))
    check(R.data_off == 0x14BF8, "KFMIRL: данные 0x%05X" % R.data_off)
    rys = R.y_axis.values(buf)
    check([round(v) for v in rys[:4]] == [440, 680, 800, 900],
          "KFMIRL: обороты по строкам, с %s" % [round(v) for v in rys[:4]])
    rows = M.read_phys(buf, R)
    check(all(rows[r][c] <= rows[r][c + 1] + 1e-6
              for r in range(R.ny) for c in range(R.nx - 1)),
          "KFMIRL: по строке наполнение растёт с требуемым моментом")
    check(rows[0][0] == 0 and abs(rows[0][-1] - 249.2) < 0.2,
          "KFMIRL: первая строка от %g до %.1f" % (rows[0][0], rows[0][-1]))

    D = geometry.resolve(a2l, "KFMDS", buf, am)
    check((D.nx, D.ny) == (12, 10), "KFMDS: %dx%d" % (D.nx, D.ny))
    dys = D.y_axis.values(buf)
    check([round(v) for v in dys[:3]] == [680, 800, 1240],
          "KFMDS: обороты по строкам, с %s" % [round(v) for v in dys[:3]])

    # -- кривая с синтетической осью
    C = geometry.resolve(a2l, "SGA08MDUB", buf, am)
    check(C.ctype == "CURVE" and C.ny == 1,
          "SGA08MDUB: кривая %dx%d" % (C.nx, C.ny))

    # -- ни одна карта не должна выходить за образ
    lays = geometry.resolve_all(a2l, buf, am)
    bad = [L2.name for L2 in lays.values()
           if L2.end > len(buf) or L2.data_off < 0]
    check(not bad, "карт за пределами образа: %d %s" % (len(bad), bad[:5]))

    # -- нелинейные пересчёты помечены нередактируемыми
    nonlin = [n for n, c in a2l.compu.items() if not c.linear]
    print("   (нелинейных пересчётов: %d)" % len(nonlin))

    print()
    if fails:
        print("ПРОВАЛЕНО: %d" % len(fails))
        return 1
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
