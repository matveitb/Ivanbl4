#!/usr/bin/env python3
"""
Проверки на реальных прошивках FBH3ID60.

Фиксируют установленные факты, чтобы правки инструментов их не сломали:
  * строка идентификации блока;
  * алгоритм и таблица контрольных сумм (32 записи сходятся);
  * геометрия трёх карт зажигания (адрес, ширина, высота);
  * дифф двух csok покрывает все три карты зажигания;
  * ни один инструмент не падает на реальном образе.

Пропускается, если прошивок нет в firmware/.
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import fwlib          # noqa: E402
import fwdiff         # noqa: E402
import gridscan       # noqa: E402
import bosch_csum     # noqa: E402
import axisscan       # noqa: E402

STOCK = os.path.join(ROOT, "firmware", "FBH3ID60_stok.bin")
CSOK1 = os.path.join(ROOT, "firmware", "FBH3ID60 e2 tun csok.bin")
CSOK2 = os.path.join(ROOT, "firmware", "FBH3ID60 e2 tun csok v2___.bin")

# адрес -> (ширина, высота)
IGN_MAPS = {
    0x10529: (11, 16),
    0x10A01: (12, 15),
    0x10AB5: (12, 16),
}


def main() -> int:
    missing = [p for p in (STOCK, CSOK1, CSOK2) if not os.path.exists(p)]
    if missing:
        print("ПРОПУСК: нет файлов прошивок:")
        for p in missing:
            print("   ", p)
        return 0

    fails = []

    def check(cond, msg):
        print("  %s %s" % ("OK  " if cond else "ОШИБКА", msg))
        if not cond:
            fails.append(msg)

    # --- размер и идентификация ---------------------------------------
    print("Идентификация:")
    fw = fwlib.load(STOCK)
    check(len(fw) == 512 * 1024, "размер стока 512 КБ")
    ident = fw.data[0x10182:0x101E0].decode("latin1")
    check("0261208504" in ident, "номер блока Bosch 0261208504")
    check("1037368793" in ident, "номер ПО 1037368793")
    check("FBH3ID60" in ident, "калибровка FBH3ID60")

    # --- контрольные суммы --------------------------------------------
    print("\nКонтрольные суммы:")
    for path in (STOCK, CSOK1, CSOK2):
        f = fwlib.load(path)
        bad, skipped, good = bosch_csum.check(f, bosch_csum.TABLE_ADDR, verbose=False)
        check(len(good) == 32 and len(bad) == 0,
              "%s: 32 записи сходятся, неверных 0 (получено %d/%d)"
              % (os.path.basename(path), len(good), len(bad)))

    # пересчёт после правки
    print("\nПересчёт сумм после правки:")
    f = fwlib.load(CSOK1)
    buf = bytearray(f.data)
    buf[0x10529] = (buf[0x10529] + 7) & 0xFF
    broken = fwlib.Firmware(bytes(buf), path="<изменённый>")
    bad, _, _ = bosch_csum.check(broken, bosch_csum.TABLE_ADDR, verbose=False)
    check(len(bad) == 1, "правка байта ломает ровно одну сумму")
    fixed_buf, n = bosch_csum.fix(broken, bosch_csum.TABLE_ADDR)
    bad2, _, good2 = bosch_csum.check(
        fwlib.Firmware(bytes(fixed_buf)), bosch_csum.TABLE_ADDR, verbose=False)
    check(n == 1 and not bad2 and len(good2) == 32, "пересчёт восстанавливает все суммы")

    # --- геометрия карт зажигания -------------------------------------
    print("\nГеометрия карт зажигания (gridscan):")
    for addr, (w, h) in IGN_MAPS.items():
        grids = gridscan.scan(fw, addr - 0x40, addr + w * h + 0x40,
                              wmin=4, wmax=32, probe=96, step=8,
                              max_score=12.0, min_margin=1.25, min_rows=4,
                              tol_abs=6.0, tol_rel=0.45)
        hit = [g for g in grids if g.addr == addr]
        ok = bool(hit) and hit[0].width == w and hit[0].rows == h
        got = ("%dx%d @0x%X" % (hit[0].width, hit[0].rows, hit[0].addr)) if hit else "не найдена"
        check(ok, "карта @0x%05X = %dx%d (получено %s)" % (addr, w, h, got))

    # --- дифф покрывает карты зажигания -------------------------------
    print("\nДифф csok1 vs csok2:")
    a, b = fwlib.load(CSOK1), fwlib.load(CSOK2)
    regions = fwdiff.diff_regions(a.data, b.data, merge_gap=16)
    fwdiff.classify(regions)
    for addr, (w, h) in IGN_MAPS.items():
        lo, hi = addr, addr + w * h
        hit = any(r.start < hi and lo < r.end for r in regions)
        check(hit, "изменения затрагивают карту @0x%05X" % addr)
    csum = [r for r in regions if r.start >= 0x1FC00]
    check(bool(csum), "правка контрольных сумм обнаружена в таблице 0x1FC00")

    # --- цепочки осей --------------------------------------------------
    print("\nОси:")
    chains = axisscan.scan_chains(fw, 0x10000, 0x1C000, min_links=4)
    total = sum(len(c) for c in chains)
    check(len(chains) == 2 and total == 45,
          "две цепочки осей, всего 45 осей (получено %d / %d)" % (len(chains), total))

    print()
    if fails:
        print("ПРОВАЛЕНО проверок: %d" % len(fails))
        return 1
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
