#!/usr/bin/env python3
"""
Проверка damoscore на единственном известном нам DAMOS.

px5ns03d.dam -- описание от родственного блока M7.9. Про него точно
известно (найдено вручную и подтверждено дизассемблером), что группа
отсечки топлива лежит в FBH3ID60 со сдвигом +1: DNSAH оказывается на
0x10EDA, DNSAL на 0x10EDB и так далее.

Если damoscore перестанет это воспроизводить -- значит сломалась либо
привязка по ссылкам кода, либо проверка допусков.
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import damoscore      # noqa: E402

DAM = os.path.join(ROOT, "firmware", "px5ns03d.dam")
FW = os.path.join(ROOT, "firmware", "FBH3ID60_stok.bin")
REFS = os.path.join(ROOT, "out", "calref.json")

# адрес -> имя, проверено вручную и дизассемблером (docs/14)
EXPECT = {0x10EDA: "DNSAH", 0x10EDB: "DNSAL", 0x10EDD: "DNSLL",
          0x10EE3: "TKATSA"}

fails = []


def check(cond, msg):
    print(("OK   " if cond else "ОШИБКА ") + msg)
    if not cond:
        fails.append(msg)


def main():
    for p in (DAM, FW, REFS):
        if not os.path.exists(p):
            print("нет файла %s -- проверка пропущена" % p)
            return 0

    objs = damoscore.load_dam(DAM)
    check(len(objs) > 2000, "объектов в калибровочном сегменте: %d" % len(objs))
    check(any(o.name == "DNSAH" for o in objs), "DNSAH есть в DAMOS")

    fw = open(FW, "rb").read()
    refs = {int(k, 16) for k in json.load(open(REFS))}
    s = damoscore.score(objs, refs, fw, min_len=6, max_gap=4, span=0x800,
                        min_rate=1.0, range_rate=0.9)

    check(s["anchored"] >= 3,
          "заякорено цепочек: %d (ждём не меньше 3)" % s["anchored"])
    shifts = dict(s["top_shifts"])
    check(1 in shifts, "найден сдвиг +1 (группа отсечки топлива)")
    check(1147 in shifts, "найден сдвиг +1147 (контур детонации)")

    for addr, name in EXPECT.items():
        got = s["placed"].get(addr)
        check(got == name,
              "0x%05X -> %s (получено %s)" % (addr, name, got))

    # A2L-ветка: наш собственный файл должен читаться без падений
    a2l = os.path.join(ROOT, "results", "FBH3ID60_legacy_verified.a2l")
    if os.path.exists(a2l):
        objs2 = damoscore.load_a2l(a2l)
        check(len(objs2) > 100, "A2L разобран: %d объектов" % len(objs2))
        dn = next((o for o in objs2 if o.name == "DNSAH"), None)
        check(dn is not None and dn.off == 0x10EDA and dn.factor == 40.0,
              "DNSAH из A2L: адрес и масштаб восстановлены")

    print()
    if fails:
        print("ПРОВАЛЕНО: %d" % len(fails))
        return 1
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
