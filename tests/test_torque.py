#!/usr/bin/env python3
"""
Проверка моментной модели.

Две карты были записаны с неверными адресами и оценкой «сомнительная»:
KFMIRL стояла на 0x14B84, KFMDS на 0x150D4. Настоящие адреса найдены по
сдвигу +86, который доказан на подтверждённой KFMIOP, и проверены
физикой. Тест закрепляет и исправление, и физику.

Самая сильная проверка -- ETALAM. Ось лямбды обязана дать РОВНО 1.000 в
точке 128, и КПД там обязан быть РОВНО 100.0 %. Два независимых попадания
в круглые числа подтверждают оба масштаба сразу: при неверных они легли бы
на произвольные точки.
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
    tm = prof["torque_model"]
    it = {i["name"]: i for i in tm["items"]}

    def word(a):
        return int.from_bytes(fw[a:a + 2], "little")

    # -- сдвиг области, на котором всё держится
    check(0x14A10 - 0x149BA == 86,
          "сдвиг области 0x14xxx: KFMIOP 0x149BA -> 0x14A10 даёт +86")

    for nm, dam in (("KFMIRL", 0x14B66), ("KFMDS", 0x150BC)):
        e = it[nm]
        H = int(e["header"], 16)
        check(H == dam + 86, "%s: дамос 0x%05X + 86 = 0x%05X" % (nm, dam, H))
        nx, ny = word(H), word(H + 2)
        check((nx, ny) == (e["nx"], e["ny"]),
              "%s: заголовок даёт %dx%d" % (nm, nx, ny))
        d = H + 4 + nx * 2 + ny * 2
        check(d == int(e["data"], 16), "%s: данные 0x%05X" % (nm, d))

    # -- KFMIRL: с ростом оборотов на тот же момент нужно меньше наполнения
    e = it["KFMIRL"]
    H = int(e["header"], 16)
    nx, ny = word(H), word(H + 2)
    d = int(e["data"], 16)
    f = e["factor"]
    col = ny // 2
    lo = word(d + (0 * ny + col) * 2) * f
    hi = word(d + ((nx - 1) * ny + col) * 2) * f
    check(lo > hi,
          "KFMIRL: на тот же момент с ростом оборотов наполнения нужно "
          "меньше, %.1f -> %.1f %%" % (lo, hi))

    # -- KFMDS: потери растут с оборотами, падают в долях с нагрузкой
    e = it["KFMDS"]
    H = int(e["header"], 16)
    nx, ny = word(H), word(H + 2)
    d = int(e["data"], 16)
    f = e["factor"]
    row0 = [word(d + (0 * ny + c) * 2) * f for c in range(ny)]
    check(row0[0] > row0[-1],
          "KFMDS: с ростом нагрузки потери в долях падают, %.2f -> %.2f %%"
          % (row0[0], row0[-1]))
    col0 = [word(d + (r * ny + 0) * 2) * f for r in range(nx)]
    check(max(col0) > col0[0],
          "KFMDS: с ростом оборотов потери растут, максимум %.2f %%" % max(col0))
    check(d == 0x15142 and d + nx * ny * 2 == 0x15232,
          "KFMDS: данные занимают ровно 0x15142..0x15232 -- та самая область "
          "в 240 байт, которую правила прошивка L")

    # -- ETALAM: два попадания в круглые числа
    e = it["ETALAM"]
    H = int(e["header"], 16)
    n = fw[H]
    check(n == e["n"], "ETALAM: счётчик %d" % n)
    ax = [fw[H + 1 + i] for i in range(n)]
    dv = [fw[H + 1 + n + i] * e["factor"] for i in range(n)]
    lam = [v / 128 for v in ax]
    one = [i for i, v in enumerate(lam) if abs(v - 1.0) < 1e-9]
    check(len(one) == 1, "ETALAM: ось даёт РОВНО лямбду 1.000 в одной точке")
    check(abs(dv[one[0]] - 100.0) < 1e-9,
          "ETALAM: при лямбде 1.000 КПД РОВНО 100.0 %% -- этим подтверждены "
          "оба масштаба сразу")
    peak = dv.index(max(dv))
    check(lam[peak] < 1.0,
          "ETALAM: максимум %.1f %% при лямбде %.3f -- момент максимален на "
          "слегка богатой" % (dv[peak], lam[peak]))
    check(dv[0] < dv[peak] and dv[-1] < dv[peak],
          "ETALAM: падает в обе стороны от максимума")

    print()
    if fails:
        print("ПРОВАЛЕНО: %d" % len(fails))
        return 1
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
