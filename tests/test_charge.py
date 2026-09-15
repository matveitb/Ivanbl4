#!/usr/bin/env python3
"""
Проверка находок по цикловому наполнению.

Главная -- KUMSRL. Её ценность в том, что значение проверяется физикой,
а не совпадением с чужим дамосом: константа жёстко задана рабочим
объёмом двигателя. Формула сначала сверяется на чужом образе, где ответ
известен, и только потом применяется к нашему.
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

RHO = 1.293          # плотность воздуха при 0 C и 1013 мбар, кг/м3
KUMSRL_F = 7.8125e-06

fails = []


def check(cond, msg):
    print(("OK   " if cond else "ОШИБКА ") + msg)
    if not cond:
        fails.append(msg)


def displacement(raw):
    """Рабочий объём в литрах, обратным счётом из KUMSRL."""
    return raw * KUMSRL_F * 1e5 / (RHO * 30)


def main():
    prof = json.load(open(os.path.join(ROOT, "profiles", "FBH3ID60.json"),
                          encoding="utf-8"))
    cc = prof["cyclic_charge"]
    kum = next(i for i in cc["items"] if i["name"] == "KUMSRL")
    check(kum["addr"] == "0x1157C", "KUMSRL записана на 0x1157C")

    # формула сначала на чужом образе, где ответ известен
    src = os.path.join(ROOT, "out", "px5ns03d_flash.bin")
    if os.path.exists(src):
        raw = open(src, "rb").read()[0x11101]
        v = displacement(raw)
        check(abs(v - 1.5) < 0.02,
              "формула на px5ns03d: raw=%d -> %.3f л (ждём ~1.5)" % (raw, v))

    fw = open(os.path.join(ROOT, "firmware", "FBH3ID60_stok.bin"), "rb").read()
    raw = fw[0x1157C]
    check(raw == kum["raw"], "сырое значение в стоке = %d" % raw)
    v = displacement(raw)
    check(abs(v - 1.594) / 1.594 < 0.01,
          "объём по KUMSRL: %.4f л против паспортных 1.594 л (%.2f %%)"
          % (v, abs(v - 1.594) / 1.594 * 100))

    # MSALLMN < MSALLMX -- это условие и отсеяло ложные попадания
    mn = next(i for i in cc["items"] if i["name"] == "MSALLMN")["value"]
    mx = next(i for i in cc["items"] if i["name"] == "MSALLMX")["value"]
    check(mn < mx, "MSALLMN (%.1f) < MSALLMX (%.1f)" % (mn, mx))
    check(abs(fw[0x1157D] * 0.1 - mn) < 1e-9 and
          abs(fw[0x1157E] * 0.1 - mx) < 1e-9,
          "пределы адаптации совпадают с образом")

    # Константа железа -- обязана быть одинаковой во всех прошивках ЭТОГО
    # блока. Отбираем по строке идентификации, а не по имени файла: в
    # firmware/ лежат и образы других блоков, где по этому адресу лежит
    # совсем другое, и это не ошибка, а другой мотор.
    import glob
    same = total = 0
    for p in sorted(glob.glob(os.path.join(ROOT, "firmware", "*.bin"))):
        b = open(p, "rb").read()
        if len(b) != len(fw) or b"FBH3ID60" not in b[0x10182:0x101E0]:
            continue
        total += 1
        same += b[0x1157C] == raw
    check(total >= 10, "образов FBH3ID60 найдено: %d" % total)
    check(same == total,
          "KUMSRL одинакова во всех %d образах FBH3ID60" % total)

    # KLAF -- побайтовое совпадение с px5ns03d
    sv = prof["saint_venant"]
    a = int(sv["addr"], 16)
    check(int.from_bytes(fw[a:a + 2], "little") == 65535,
          "KLAF начинается с 65535")
    if os.path.exists(src):
        s = open(src, "rb").read()
        pts = [0, 50, 100, 150, 200, 250, 300, 350, 400, 450, 500]
        ours = [int.from_bytes(fw[a + 2 * i:a + 2 * i + 2], "little")
                for i in pts]
        theirs = [int.from_bytes(s[0x15460 + 2 * i:0x15460 + 2 * i + 2],
                                 "little") for i in pts]
        check(ours == theirs,
              "KLAF совпадает с px5ns03d во всех %d контрольных точках"
              % len(pts))
        check(a - 0x15460 == sv["region_shift"],
              "сдвиг области 0x15xxx = +%d" % sv["region_shift"])

    # KFMSNWDK: заголовок Bosch, оси внутри карты
    tam = prof.get("throttle_air_model", {})
    kf = next((i for i in tam.get("items", []) if i["name"] == "KFMSNWDK"), None)
    if kf:
        H = int(kf["header"], 16)
        nx = int.from_bytes(fw[H:H + 2], "little")
        ny = int.from_bytes(fw[H + 2:H + 4], "little")
        check((nx, ny) == (kf["nx"], kf["ny"]),
              "заголовок 0x%05X даёт %dx%d" % (H, nx, ny))
        d = int(kf["data"], 16)
        check(d == H + 4 + nx * 2 + ny * 2,
              "данные начинаются сразу за осями: 0x%05X" % d)
        # физика: расход обязан расти по отношению давлений в каждом столбце
        def cell(r, c):
            a = d + (r * ny + c) * 2
            return int.from_bytes(fw[a:a + 2], "little")
        mono = all(cell(r, c) <= cell(r + 1, c)
                   for r in range(nx - 1) for c in range(ny))
        check(mono, "расход растёт по отношению давлений во всех столбцах")
        check(max(cell(r, c) for r in range(nx) for c in range(ny)) * 0.1 < 6554,
              "значения в допуске дамоса 0..6554 кг/ч")
        # тот же сдвиг области, что у KLAF
        check(H - 0x15370 == prof["saint_venant"]["region_shift"],
              "сдвиг KFMSNWDK совпал со сдвигом KLAF: +%d"
              % (H - 0x15370))

    # KUMSRL: формула Bosch из Funktionsrahmen -- KUMSRL = V / 2578.
    # Проверяем, что мой вывод из плотности воздуха ей эквивалентен.
    fr = 1.0 / 2578
    mine = 1.293 * 30 / 1e5
    check(abs(fr - mine) / fr < 0.001,
          "формула ФР V/2578 = %.6g совпала с выводом из плотности %.6g"
          % (fr, mine))
    check(abs(raw * KUMSRL_F * 2578 - 1.594) / 1.594 < 0.01,
          "объём по формуле ФР: %.4f л" % (raw * KUMSRL_F * 2578))

    # RLNOT -- подстановочное наполнение при отказе
    sub = cc.get("substitution")
    if sub:
        a = int(sub["addr"], 16)
        got = [fw[a + i] * sub["factor"] for i in range(sub["n"])]
        check(got == sub["values"], "RLNOT на 0x%05X: %s" % (a, got))
        check(all(got[i] < got[i + 1] for i in range(len(got) - 1)),
              "RLNOT растёт по оборотам -- форма аварийной кривой")
        # вплотную к подтверждённому блоку адаптации, без разрыва
        check(a == 0x1157F + 1, "RLNOT начинается сразу за MSLG")

    # ширина из метки типа пересчёта
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import damos
    dam = os.path.join(ROOT, "firmware", "px5ns03d.dam")
    if os.path.exists(dam):
        d = damos.parse(dam)
        for name, want in (("KUMSRL", (1, False)), ("KFZWOP", (1, True)),
                           ("NMAX", (2, False)), ("KFRLW", (2, False))):
            v2 = d.by_name(name)
            got = damos.width_of(d.conv(v2.conv_w))
            check(got == want, "ширина %s: %s" % (name, got))

    print()
    if fails:
        print("ПРОВАЛЕНО: %d" % len(fails))
        return 1
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
