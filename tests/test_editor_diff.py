#!/usr/bin/env python3
"""
Проверка сравнения двух прошивок.

Сверяется не «что-то нашлось», а совпадение с тем, что про эти прошивки
уже известно независимо: какие карты правил csok v2 и сколько байт
изменил вариант с поднятым углом.

Отдельно закрепляется, что таблица контрольных сумм не попадает в список
изменённых карт: она меняется при любой правке, и если её показывать, в
каждом сравнении будет ложная строка.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "editor", "a2l"))
sys.path.insert(0, os.path.join(ROOT, "editor", "core"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import compare         # noqa: E402
import geometry        # noqa: E402
import model           # noqa: E402

A2L = os.path.join(ROOT, "results", "FBH3ID60_legacy.a2l")
STOCK = os.path.join(ROOT, "firmware", "FBH3ID60_stok.bin")
CSOK2 = os.path.join(ROOT, "firmware", "FBH3ID60 e2 tun csok v2___.bin")
ZWOP = os.path.join(ROOT, "results", "FBH3ID60_csok_v2_zwop+2.25_7000.bin")

fails = []


def check(cond, msg):
    print(("OK   " if cond else "ОШИБКА ") + msg)
    if not cond:
        fails.append(msg)


def main():
    a2l = model.load(A2L)
    stock = open(STOCK, "rb").read()
    csok = open(CSOK2, "rb").read()
    am = geometry.detect_addressing(a2l, len(stock))

    res = compare.compare_files(a2l, stock, csok, am)
    names = {d.name for d in res.maps}

    # -- те самые карты, про которые известно, что csok v2 их правил
    for nm in ("KFZW", "KFZW2", "KFZWOP", "KFZWMS", "KFZWMN", "KFLBTS",
               "KFMIOP"):
        check(nm in names, "csok v2 изменил %s" % nm)

    # -- таблица сумм не должна выглядеть как изменённая карта
    check(res.checksum_bytes > 0,
          "байт в таблице контрольных сумм: %d (учтены отдельно)"
          % res.checksum_bytes)
    csum_named = [d.name for d in res.maps
                  if 0x1FC00 <= d.layout.data_off < 0x20000]
    check(not csum_named,
          "таблица сумм не попала в список карт: %s" % csum_named)

    # -- углы подняты, а не опущены
    zw = next(d for d in res.maps if d.name == "KFZW")
    check(zw.dmin > 0, "KFZW: все изменения в плюс, минимум %+.2f град"
          % zw.dmin)
    check(zw.unit == "grad KW", "KFZW: единицы %s" % zw.unit)

    # -- вариант с поднятым углом: ровно 39 байт в KFZWOP
    if os.path.exists(ZWOP):
        zwop_bin = open(ZWOP, "rb").read()
        r2 = compare.compare_files(a2l, csok, zwop_bin, am)
        by = {d.name: d for d in r2.maps}
        check("KFZWOP" in by, "правка угла видна в KFZWOP")
        if "KFZWOP" in by:
            d = by["KFZWOP"]
            check(d.changed == 39,
                  "KFZWOP: изменено %d ячеек (ждём 39)" % d.changed)
            check(abs(d.dmax - 2.25) < 1e-9 and d.dmin > 0,
                  "KFZWOP: дельта %+.2f..%+.2f град" % (d.dmin, d.dmax))
        check("KFZW" not in by,
              "базовая KFZW не тронута -- поднимали только оптимальный угол")

    print()
    if fails:
        print("ПРОВАЛЕНО: %d" % len(fails))
        return 1
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
