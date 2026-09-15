#!/usr/bin/env python3
"""
Проверка тракта ДМРВ: коррекция характеристики и коррекция пульсаций.

Пять карт найдены не выравниванием по чужому дамосу (их там нет вовсе), а
по коду: диспетчер выбирает одну из четырёх пульсационных карт по режиму
и всегда читает карту коррекции ДМРВ.

Самая убедительная проверка -- внутренняя. Два из четырёх вариантов
относятся к управлению фазами газораспределения, а фазовращателя на этом
моторе нет. Значит они обязаны быть нейтральными, то есть строго в
единицах. Случайно так совпасть не может.
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

fails = []


def check(cond, msg):
    print(("OK   " if cond else "ОШИБКА ") + msg)
    if not cond:
        fails.append(msg)


def main():
    prof = json.load(open(os.path.join(ROOT, "profiles", "FBH3ID60.json"),
                          encoding="utf-8"))
    fw = open(os.path.join(ROOT, "firmware", "FBH3ID60_stok.bin"), "rb").read()
    items = {i["name"]: i for i in prof["hfm_path"]["items"]}
    check(len(items) == 6, "объектов тракта ДМРВ записано: %d" % len(items))

    # двумерные карты и одномерные кривые проверяются по-разному
    grids = {k: v for k, v in items.items() if "nx" in v}
    check(len(grids) == 5, "из них двумерных карт: %d" % len(grids))

    for nm, it in grids.items():
        H = int(it["header"], 16)
        nx, ny = fw[H], fw[H + 1]
        check((nx, ny) == (it["nx"], it["ny"]),
              "%s: заголовок 0x%05X даёт %dx%d" % (nm, H, nx, ny))
        d = H + 2 + nx + ny
        check(d == int(it["data"], 16),
              "%s: данные сразу за осями, 0x%05X" % (nm, d))
        ax = [fw[H + 2 + i] for i in range(nx)]
        ay = [fw[H + 2 + nx + i] for i in range(ny)]
        check(all(ax[i] < ax[i + 1] for i in range(nx - 1))
              and all(ay[i] < ay[i + 1] for i in range(ny - 1)),
              "%s: обе оси строго возрастают" % nm)

    def data(nm):
        it = grids[nm]
        d = int(it["data"], 16)
        return [fw[d + i] for i in range(it["nx"] * it["ny"])]

    # варианты для фазовращателя обязаны быть нейтральными -- его тут нет
    for nm in ("KFPUNW", "KFPUSUNW"):
        vals = set(data(nm))
        check(vals == {128},
              "%s нейтральна (ровно 1.000 во всех ячейках) -- "
              "вариант для фазовращателя, которого на S6D нет" % nm)

    # а рабочие -- нет
    for nm in ("KFPU", "KFPUSU"):
        vals = set(data(nm))
        check(len(vals) > 5, "%s несёт данные: %d различных значений"
              % (nm, len(vals)))
        d = data(nm)
        check(all(0.85 <= v / 128 <= 1.25 for v in d),
              "%s в пределах множителя 0.85..1.25" % nm)

    # PUKANS: привязка ровно к единице в нуле градусов
    pk = items.get("PUKANS")
    if pk:
        H = int(pk["header"], 16)
        n = fw[H]
        check(n == pk["n"], "PUKANS: счётчик %d" % n)
        ax = [fw[H + 1 + i] * 0.75 - 48 for i in range(n)]
        dv = [fw[H + 1 + n + i] / 128 for i in range(n)]
        zero = [i for i, t in enumerate(ax) if abs(t) < 0.01]
        check(len(zero) == 1 and abs(dv[zero[0]] - 1.0) < 1e-9,
              "PUKANS равна РОВНО 1.000 ровно в точке 0 C -- масштаб оси "
              "(x0.75 минус 48) подтверждён этим попаданием")
        check(all(dv[i] > dv[i + 1] for i in range(n - 1)),
              "PUKANS убывает с температурой: %.3f -> %.3f" % (dv[0], dv[-1]))

    # KFRLW: наполнение падает с оборотами при фиксированном дросселе
    tam0 = {i["name"]: i for i in prof["throttle_air_model"]["items"]}
    kr = tam0.get("KFRLW")
    if kr:
        H = int(kr["header"], 16)

        def word(a):
            return int.from_bytes(fw[a:a + 2], "little")

        nx, ny = word(H), word(H + 2)
        check((nx, ny) == (kr["nx"], kr["ny"]),
              "KFRLW: заголовок даёт %dx%d" % (nx, ny))
        d = H + 4 + nx * 2 + ny * 2
        check(d == int(kr["data"], 16), "KFRLW: данные 0x%05X" % d)
        rpm = [word(H + 4 + nx * 2 + i * 2) * 0.25 for i in range(ny)]
        check(rpm[0] == 1240 and rpm[-1] == 6000,
              "KFRLW: ось оборотов %.0f..%.0f" % (rpm[0], rpm[-1]))
        f = kr["factor"]
        # при фиксированном открытии дросселя наполнение обязано падать с
        # оборотами -- цилиндр не успевает наполниться
        mid = nx // 2
        row = [word(d + (mid * ny + c) * 2) * f for c in range(ny)]
        check(row[0] > row[-1],
              "KFRLW: при фиксированном дросселе наполнение падает с "
              "оборотами, %.1f -> %.1f %%" % (row[0], row[-1]))
        top = [word(d + ((nx - 1) * ny + c) * 2) * f for c in range(ny)]
        check(85 <= max(top) <= 100,
              "KFRLW: при широком дросселе упирается в %.1f %% -- сходится "
              "с RLVMXN (79..92 %%)" % max(top))

    # WDKUGDN: растёт по оборотам
    tam = {i["name"]: i for i in prof["throttle_air_model"]["items"]}
    wd = tam.get("WDKUGDN")
    if wd:
        a = int(wd["addr"], 16)
        v = [int.from_bytes(fw[a + i * 2:a + i * 2 + 2], "little")
             * wd["factor"] for i in range(wd["n"])]
        check(all(abs(v[i] - wd["values"][i]) < 0.01 for i in range(len(v))),
              "WDKUGDN на 0x%05X совпал с записанным" % a)
        check(v[-1] > v[0] and all(0 <= x <= 100 for x in v),
              "WDKUGDN растёт по оборотам, %.1f -> %.1f %%" % (v[0], v[-1]))

    print()
    if fails:
        print("ПРОВАЛЕНО: %d" % len(fails))
        return 1
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
