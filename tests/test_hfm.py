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
    check(len(items) == 5, "карт тракта ДМРВ записано: %d" % len(items))

    for nm, it in items.items():
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
        it = items[nm]
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
