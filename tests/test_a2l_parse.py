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
    # Порог опущен дважды: сперва убраны карты, лезущие на чужие байты,
    # потом -- все НЕПОДТВЕРЖДЁННЫЕ. Осталось то, на что есть ссылка из
    # кода. Меньше карт, но каждая чем-то подтверждена.
    check(len(a2l.characteristics) > 500,
          "карт разобрано: %d" % len(a2l.characteristics))
    check(len(a2l.axis_pts) >= 6, "осей-объектов: %d" % len(a2l.axis_pts))
    check(len(a2l.compu) > 60, "пересчётов: %d" % len(a2l.compu))
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

    # -- ВСЕ оси обязаны возрастать
    #
    # Проверка общая нарочно. Точечных было три -- KFZWOP, KFMIRL, KFMDS --
    # и они пропустили сломанные оси KFMIOP: ось оборотов читалась как
    # 113280, 205480 вместо 440, 680, потому что генератор принял начало
    # блока со счётчиком за начало точек. Пятнадцать зелёных наборов этого
    # не заметили. Ось, идущая не по возрастанию, физического смысла не
    # имеет ни у одной карты, поэтому проверяем разом по всем.
    lays = geometry.resolve_all(a2l, buf, am)
    crooked = []
    for L2 in lays.values():
        for tag, src in (("X", L2.x_axis), ("Y", L2.y_axis)):
            if src.kind not in ("inline", "ref") or src.count < 2:
                continue
            v = src.values(buf)
            if any(v[i + 1] <= v[i] for i in range(len(v) - 1)):
                crooked.append("%s.%s %s" % (L2.name, tag,
                                             [round(x, 1) for x in v[:4]]))
    check(not crooked, "осей, идущих не по возрастанию: %d %s"
          % (len(crooked), crooked[:3]))

    # -- KFMIOP закрепляем поимённо: именно её я и сломал
    P = geometry.resolve(a2l, "KFMIOP", buf, am)
    check((P.nx, P.ny) == (11, 16), "KFMIOP: %dx%d" % (P.nx, P.ny))
    pxs, pys = P.x_axis.values(buf), P.y_axis.values(buf)
    check([round(v) for v in pys[:3]] == [440, 680, 800]
          and round(pys[-1]) == 6520,
          "KFMIOP: обороты по строкам %s .. %g"
          % ([round(v) for v in pys[:3]], pys[-1]))
    check([round(v) for v in pxs[:3]] == [10, 15, 20] and round(pxs[-1]) == 100,
          "KFMIOP: нагрузка по столбцам %s .. %g"
          % ([round(v) for v in pxs[:3]], pxs[-1]))

    # -- карты не должны делить байты
    #
    # Две карты на одних байтах не могут быть верны обе, а в редакторе это
    # прямая порча: правишь одну, меняется другая. Разрешение по весу
    # доказательства оставляет только случаи, где обе стороны подтверждены
    # -- их перечисляем поимённо, чтобы новое перекрытие не проскочило под
    # общим порогом.
    # СПИСОК ПУСТ, и это главное в этой проверке.
    #
    # Было четыре пары «подтверждённая против подтверждённой», и все
    # четыре оказались не спором, а ошибкой -- каждый раз в записи из
    # описи, то есть из выравнивания чужого дамоса:
    #
    #   KFBAKL + KRKTE     -- у KRKTE был чужой адрес, код читает 0x1955A
    #   KFKHFM + KFWKSTT   -- KFWKSTT лезла через границу двух карт
    #   KLAF + KFMSNTAG    -- KFMSNTAG внутри KLAF, и опять мнимый EGR
    #   KFMSNWDK + LLSPMSN -- LLSPMSN внутри, да ещё по нечётному смещению
    #
    # Пустой список -- это утверждение: ни одна карта в описании не делит
    # байты ни с какой другой. Появится пара -- проверка упадёт, и это
    # правильно: разбирать надо, а не заносить в исключения.
    KNOWN = set()
    ordered = sorted((L2 for L2 in lays.values() if L2.size),
                     key=lambda L2: L2.data_off)
    shared = []
    for i in range(len(ordered) - 1):
        A2, B2 = ordered[i], ordered[i + 1]
        if A2.end > B2.data_off:
            pair = tuple(sorted((A2.name, B2.name)))
            if pair not in KNOWN:
                shared.append("%s + %s" % pair)
    check(not shared, "карт, делящих байты сверх известных %d: %d %s"
          % (len(KNOWN), len(shared), shared[:4]))

    # -- блок кондиционера: кривая с НАСТОЯЩЕЙ осью
    #
    # Раньше на этих байтах стояла "карта регулятора AGR" 7x7 -- имя из
    # выравнивания, само же помеченное "не сходится". По коду там ось
    # оборотов и четыре кривые по восемь точек. Закрепляем поимённо две
    # вещи, на которых ошибка была бы не видна глазом: ось должна
    # начинаться с 800 (адрес указывает на СЧЁТЧИК, а не на точки -- с
    # адресом точек первая уезжала в конец), и пороги дросселя должны
    # читаться в процентах, а не в сырых числах.
    A = geometry.resolve(a2l, "WDKKOEN", buf, am)
    check(A.ctype == "CURVE" and (A.nx, A.ny) == (8, 1),
          "WDKKOEN: кривая %dx%d" % (A.nx, A.ny))
    axs = A.x_axis.values(buf)
    check([round(v) for v in axs] == [800, 1000, 1400, 2000, 3000,
                                      4000, 5000, 6000],
          "WDKKOEN: ось оборотов %s" % [round(v) for v in axs])
    wd = M.read_phys(buf, A)[0]
    check(abs(wd[0] - 23.05) < 0.02 and abs(wd[-1] - 50.0) < 0.02,
          "WDKKOEN: порог дросселя %.2f..%.2f %%" % (wd[0], wd[-1]))
    T = geometry.resolve(a2l, "TMKOAO", buf, am)
    check(abs(M.read_phys(buf, T)[0][0] - 114.75) < 0.01,
          "TMKOAO: верхний порог t ОЖ %.2f" % M.read_phys(buf, T)[0][0])
    check("KFAGRP" not in a2l.characteristics,
          "KFAGRP выброшена: на её байтах лежит TKOAMNN")

    # -- топливо: адрес KRKTE и ось кривых лямбды
    #
    # KRKTE -- постоянная форсунки, на неё умножается вся подача. Адрес
    # взят не из выравнивания, а из чтения кода, и это проверяется здесь
    # поимённо, потому что ошибка в один байт тут ничем себя не выдаёт:
    # соседние ячейки тоже правдоподобные числа.
    K = geometry.resolve(a2l, "KRKTE", buf, am)
    check(K.data_off == 0x1955A, "KRKTE: адрес 0x%05X" % K.data_off)
    check(abs(M.read_phys(buf, K)[0][0] - 0.1655) < 0.001,
          "KRKTE: %.4f мс/%%" % M.read_phys(buf, K)[0][0])
    check(abs(M.read_phys(buf, geometry.resolve(a2l, "FRAOMX", buf, am))[0][0]
              - 1.25) < 0.001, "FRAOMX: верхний предел коррекции 1.25")

    # -- кривые лямбды по оборотам обязаны БЫТЬ в описании
    #
    # Проверка покрытия (tools/coverage.py) поймала, что самого правленого
    # места во всей прошивке в A2L не было вовсе: в профиле кривые лежали
    # свободной формой, которую сборщик не читает. Закрепляем поимённо --
    # и ось, потому что она была записана температурной, а на деле по
    # оборотам.
    for nm, off in (("KLLAMFA_0", 0x19562), ("KLLAMFA_1", 0x1956F)):
        C2 = geometry.resolve(a2l, nm, buf, am)
        check(C2.data_off == off and C2.nx == 6,
              "%s: данные 0x%05X, точек %d" % (nm, C2.data_off, C2.nx))
        cax = [round(v) for v in C2.x_axis.values(buf)]
        check(cax == [1480, 2480, 3480, 4480, 5480, 6480],
              "%s: ось по оборотам %s" % (nm, cax))
        check(abs(M.read_phys(buf, C2)[0][0] - 1.0) < 1e-9,
              "%s: сток -- ровно 1.000" % nm)

    # -- полная нагрузка: порог и обогащение
    #
    # Две половины одной цепочки, и правят их все десять авторов. Порог
    # закрепляем вместе с осью: обе кривые лежат В ОДНОМ блоке подряд, и
    # сдвиг на семь байт дал бы соседнюю -- значения там те же, ошибка
    # молчала бы.
    W0 = geometry.resolve(a2l, "WDKVLN_0", buf, am)
    check(W0.data_off == 0x18FDE and W0.nx == 6,
          "WDKVLN_0: данные 0x%05X, точек %d" % (W0.data_off, W0.nx))
    check(abs(M.read_phys(buf, W0)[0][0] - 65.1) < 0.1,
          "WDKVLN_0: порог %.1f %%" % M.read_phys(buf, W0)[0][0])
    S = geometry.resolve(a2l, "WDKSLN", buf, am)
    check(abs(M.read_phys(buf, S)[0][0] - 0.78) < 0.02,
          "WDKSLN: порог холостого %.2f %%" % M.read_phys(buf, S)[0][0])
    for nm in ("KLLAMFA_0", "KLLAMFA_1"):
        F = geometry.resolve(a2l, nm, buf, am)
        check([round(v) for v in F.x_axis.values(buf)]
              == [1480, 2480, 3480, 4480, 5480, 6480],
              "%s: ось по оборотам" % nm)
        check(abs(M.read_phys(buf, F)[0][0] - 1.0) < 1e-9,
              "%s: сток -- ровно 1.000" % nm)

    # -- у оси бывает не только множитель, но и смещение
    #
    # Ось по температуре ОЖ читалась 17.25..147 вместо -30.75..99:
    # множитель применялся, вычитание 48 -- нет. На осях оборотов это
    # незаметно, там смещения нет, поэтому ошибка и прошла.
    T = geometry.resolve(a2l, "RLLRTMO", buf, am)
    tax = [round(v, 2) for v in T.x_axis.values(buf)]
    check(tax[0] == -30.75 and tax[-1] == 99.0,
          "RLLRTMO: ось по температуре %s .. %s" % (tax[0], tax[-1]))

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
