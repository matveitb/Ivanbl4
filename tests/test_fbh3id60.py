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
import damos          # noqa: E402

STOCK = os.path.join(ROOT, "firmware", "FBH3ID60_stok.bin")
CSOK1 = os.path.join(ROOT, "firmware", "FBH3ID60 e2 tun csok.bin")
CSOK2 = os.path.join(ROOT, "firmware", "FBH3ID60 e2 tun csok v2___.bin")

DAMOS = os.path.join(ROOT, "firmware", "px5ns03d.dam")

# Карты зажигания FBH3ID60: адрес -> (ширина, высота).
# Геометрия и имена подтверждены заводским DAMOS родственной прошивки.
IGN_MAPS = {
    0x10529: (11, 16),   # KFZWOP
    0x109F5: (12, 16),   # KFZW
    0x10AB5: (12, 16),   # KFZW2
    0x10935: (12, 16),   # KFZWMS
}
# Те, что детектор ширины находит точно и самостоятельно
GRID_DETECTABLE = {0x10529: (11, 16), 0x10AB5: (12, 16)}
# Между двумя csok правились только эти три; KFZWMS менялся относительно
# стока, но между csok1 и csok2 он одинаков.
IGN_CHANGED_BETWEEN_CSOK = {0x10529: (11, 16), 0x109F5: (12, 16), 0x10AB5: (12, 16)}


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
    for addr, (w, h) in GRID_DETECTABLE.items():
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
    for addr, (w, h) in IGN_CHANGED_BETWEEN_CSOK.items():
        lo, hi = addr, addr + w * h
        hit = any(r.start < hi and lo < r.end for r in regions)
        check(hit, "изменения затрагивают карту @0x%05X" % addr)
    kms = 0x10935
    hit = any(r.start < kms + 12 * 16 and kms < r.end for r in regions)
    check(not hit, "KFZWMS между двумя csok не менялся")

    # а относительно стока KFZWMS правился
    st = fwlib.load(STOCK)
    reg2 = fwdiff.diff_regions(st.data, a.data, merge_gap=16)
    hit2 = any(r.start < kms + 12 * 16 and kms < r.end for r in reg2)
    check(hit2, "KFZWMS изменён относительно стока")
    csum = [r for r in regions if r.start >= 0x1FC00]
    check(bool(csum), "правка контрольных сумм обнаружена в таблице 0x1FC00")

    # --- цепочки осей --------------------------------------------------
    print("\nОси:")
    chains = axisscan.scan_chains(fw, 0x10000, 0x1C000, min_links=4)
    total = sum(len(c) for c in chains)
    check(len(chains) == 2 and total == 45,
          "две цепочки осей, всего 45 осей (получено %d / %d)" % (len(chains), total))

    # --- DAMOS -----------------------------------------------------------
    if os.path.exists(DAMOS):
        print("\nDAMOS (px5ns03d):")
        d = damos.parse(DAMOS)
        check(len(d.variables) == 2447, "разобрано 2447 переменных (получено %d)"
              % len(d.variables))
        check(len(d.conversions) == 333, "разобрано 333 пересчёта (получено %d)"
              % len(d.conversions))
        segs = {n: (a, b) for n, a, b in d.segments}
        check(segs.get("DATA1") == (0x810000, 0x81FFFF),
              "сегмент DATA1 = $810000..$81FFFF")
        zwop = d.by_name("KFZWOP")
        check(zwop is not None and zwop.file_offset == 0x10528,
              "KFZWOP по DAMOS лежит в исходной прошивке по 0x10528")
        if zwop:
            w, h = d.dims(zwop)
            check((w, h) == (11, 16), "KFZWOP имеет размер 11x16 (получено %dx%d)" % (w, h))
            cw = d.conv(zwop.conv_w)
            check(cw is not None and abs(cw.factor - 0.75) < 1e-9,
                  "масштаб угла зажигания 0.75 град/ед. (получено %.6g)"
                  % (cw.factor if cw else -1))
            cx = d.conv(zwop.conv_x)
            check(cx is not None and abs(cx.factor - 40.0) < 1e-9,
                  "масштаб оборотов 40 об/мин на единицу (получено %.6g)"
                  % (cx.factor if cx else -1))

        # карты зажигания в FBH3ID60 читаются в разумном диапазоне градусов
        print("\nКарты зажигания в физических единицах:")
        for addr, (w, h), name in ((0x10529, (11, 16), "KFZWOP"),
                                   (0x109F5, (12, 16), "KFZW"),
                                   (0x10AB5, (12, 16), "KFZW2")):
            vals = fw.vec(addr, w * h, 1, signed=True)
            lo, hi = min(vals) * 0.75, max(vals) * 0.75
            check(-30.0 <= lo and hi <= 50.0,
                  "%s в пределах -30..50 град (получено %.2f..%.2f)" % (name, lo, hi))


        # KFLBTS -- лямбда на полной нагрузке. Оси и содержимое верхней строки
        # (газ в пол) зафиксированы: в стоке до 3200 об/мин стоит ровно 1.00.
        nax = fw.vec(0x181F7 + 1, 16, 1)
        lax = fw.vec(0x1821E + 1, 12, 1)
        check([v * 40 for v in nax[:5]] == [400, 800, 1200, 1600, 2000],
              "ось оборотов KFLBTS 0x181F7 начинается 400..2000 об/мин")
        check(abs(lax[-1] * 0.75 - 84.75) < 1e-9,
              "ось нагрузки KFLBTS 0x1821E доходит до 84.75 %")
        wot = fw.vec(0x19636 + 11 * 16, 16, 1)
        check(all(v == 128 for v in wot[:8]),
              "в стоке KFLBTS при полном газе = 1.00 до 3200 об/мин")
        check(wot[8] < 128 and wot[9] < wot[8],
              "обогащение включается с 3600 об/мин и дальше растёт")


        # группа ограничения оборотов по 0x14BB2: порядок из DAMOS
        u16 = lambda a: fw.vec(a, 1, 2)[0]
        check(u16(0x14BB2) * 0.25 == 120.0, "DNMAXH = 120 об/мин")
        check(u16(0x14BB4) * 0.25 == 6700.0, "NMAX в стоке = 6700 об/мин")
        check(u16(0x14BB6) * 0.25 == 6000.0, "NMAXDV в стоке = 6000 об/мин")
        check(u16(0x14BB8) * 0.25 == 1200.0, "NMXDKPU = 1200 об/мин (лимп-хоум)")
        check(u16(0x14BBA) * 0.01 == 3.0, "TNMAXDV = 3 с")


        # KFMIOP: блок 0x14A10 длиной 16*11 слов упирается ровно в ось SRL11OPUW
        mio = [fw.vec(0x14A10 + 2 * (r * 11 + c), 1, 2)[0] * 0.00152588
               for r in range(16) for c in range(11)]
        check(0x14A10 + 2 * 11 * 16 == 0x14B70,
              "KFMIOP кончается ровно там, где начинается ось 0x14B70")
        rows_ok = all(all(mio[r * 11 + c] < mio[r * 11 + c + 1] for c in range(10))
                      for r in range(16))
        check(rows_ok, "KFMIOP строго растёт по нагрузке во всех 16 строках")
        check(3.9 <= mio[0] <= 4.1 and 83.0 <= mio[-1] <= 84.0,
              "KFMIOP в стоке от 4.00 до 83.50 %% (получено %.2f..%.2f)" % (mio[0], mio[-1]))

        # ETADZW: КПД по дельте угла, 100 %% при нуле и монотонное падение
        eta = fw.vec(0x104D3, 65, 1)
        check(eta[0] * 0.5 == 100.0, "ETADZW начинается со 100 %%")
        check(all(eta[i] >= eta[i + 1] for i in range(64)), "ETADZW не возрастает")
        check(20.0 <= eta[-1] * 0.5 <= 22.0,
              "ETADZW кончается на 21.5 %% (получено %.1f)" % (eta[-1] * 0.5))


        # группа отсечки топлива при сбросе газа, найдена по ссылкам кода
        b = lambda a: fw.vec(a, 1, 1)[0]
        check(b(0x10EE3) * 5 - 50 == 950.0, "TKATSA = 950 C, как в исходной M7.9")
        check(b(0x10EE2) == 63, "ENSAKHG = 63, как в исходной M7.9")
        check(b(0x10EDF) * 40 == 520, "DNWEELLS = 520 об/мин, как в исходной M7.9")
        check(b(0x10EE0) * 40 == 200, "DNWEK = 200 об/мин, как в исходной M7.9")
        check(b(0x10EDA) * 40 == 1000 and b(0x10EDB) * 40 == 600,
              "DNSAH = 1000 и DNSAL = 600 об/мин")


        # топливный тракт: постоянная форсунки и глобальный множитель
        check(abs(fw.vec(0x19485, 1, 1)[0] * 0.00177936 - 0.1637) < 1e-3,
              "KRKTE = 0.1637 мс на %% (форсунка около 172 см3/мин)")
        check(fw.vec(0x190CD, 1, 1)[0] == 128, "FRKAP = 1.000, глобальный множитель топлива нетронут")
        check(all(v == 128 for v in fw.vec(0x190EC, 192, 1)),
              "KFLF = 1.000 во всех 192 ячейках")


        # маска применения обогащения защиты компонентов: только 0 и 1
        mask = fw.vec(0x19576, 192, 1)
        check(set(mask) == {0, 128}, "маска 0x19576 состоит только из 0.00 и 1.00")
        check(all(mask[r * 16 + c] == 128 for r in (9, 10, 11) for c in range(16)),
              "при нагрузке 75 %% и выше маска открыта на всех оборотах")
        check(mask[7 * 16 + 4] == 0 and mask[8 * 16 + 4] == 0,
              "на 2000 об/мин при 65 и 70 %% нагрузки маска закрыта")


        # KFLF: единственный блок из 192 байт 1.000 в районе смеси
        data = fw.vec(0x18000, 0x4000, 1)
        runs = [a for a in range(len(data) - 192)
                if all(data[a + i] == 128 for i in range(192))
                and (a == 0 or data[a - 1] != 128)]
        check(runs == [0x190EC - 0x18000],
              "в 0x18000..0x1C000 ровно один блок из 192 байт 1.000, и это 0x190EC")


        # блок диагностики катализатора и подогрева датчиков, сдвиг -16
        check(fw.vec(0x1896B, 1, 1)[0] * 5.0 - 50 == 650, "TMAXKAT = 650 C")
        check(fw.vec(0x1896C, 1, 1)[0] * 5.0 - 50 == 470, "TMINKAT = 470 C")
        check(abs(fw.vec(0x1898D, 1, 1)[0] * 0.0704 - 13.02) < 0.05,
              "UHSN около 13.0 В (номинал для подогрева датчика)")


        # KFLF: заголовочная карта, указатель кода ведёт на заголовок 0x190CE
        check(fw.vec(0x190CE, 1, 1)[0] == 12 and fw.vec(0x190CF, 1, 1)[0] == 16,
              "заголовок KFLF 0x190CE: nx=12, ny=16")
        check(0x190CE + 2 + 12 + 16 == 0x190EC,
              "данные KFLF начинаются ровно по 0x190EC")
        ax = fw.vec(0x190D0, 12, 1)
        ay = fw.vec(0x190DC, 16, 1)
        check(all(ax[i] < ax[i + 1] for i in range(11)), "ось X KFLF строго растёт")
        check(all(ay[i] < ay[i + 1] for i in range(15)), "ось Y KFLF строго растёт")


        # плёночная модель: четыре карты 7x9 с шагом 0x3F на общей оси 0x181E4
        check(fw.vec(0x181E4, 1, 1)[0] == 7, "ось плёночной модели 0x181E4: 7 точек")
        for name, a in (("KFABAK", 0x193F3), ("KFAVAK", 0x19432),
                        ("KFBAKL", 0x19471), ("KFVAKL", 0x194B0)):
            v = fw.vec(a, 63, 1)
            check(all(x > 0 for x in v), "%s 0x%05X: ни одной нулевой ячейки" % (name, a))
        b = fw.vec(0x19471, 63, 1)
        check(b[0] * 0.0625 > b[56] * 0.0625,
              "KFBAKL убывает от первой строки к последней")


        # контур детонации: блок 0x1163B, значения в допусках DAMOS
        check(abs(fw.vec(0x1163B, 1, 1)[0] * 0.01953125 - 0.4102) < 0.01,
              "DKROKMX = 0.41 В")
        check(fw.vec(0x11640, 1, 1)[0] == 16 and fw.vec(0x11641, 1, 1)[0] == 4,
              "KRFTP1 = 16, KRFTP2 = 4")
        check(fw.vec(0x11663, 1, 1)[0] == 2, "DYADS = -1.5 град (сырое 2)")
        check(fw.vec(0x1166B, 1, 1)[0] * 40 == 1200, "DKROKU = 1200 об/мин")
        check(fw.vec(0x11668, 1, 1)[0] * 0.75 - 48 == 80.25, "TMDYNA = 80.25 C")


        # адсорбер: район 0x19xxx со сдвигом +206
        check(fw.vec(0x1976C, 1, 1)[0] * 0.75 - 48 == 39.0,
              "TMTE = 39 C (порог температуры для продувки)")
        check(fw.vec(0x19914, 1, 1)[0] * 0.390625 == 40.0, "ETAZWTEN = 40 %%")
        # блок диагностики CPV: кодовые слова mode 6 идут подряд 1..7
        tc6 = fw.vec(0x1996E, 7, 1)
        check(list(tc6) == [1, 6, 7, 3, 5, 4, 2],
              "кодовые слова TC6TEC* 0x1996E..0x19974 = 1,6,7,3,5,4,2")

    print()
    if fails:
        print("ПРОВАЛЕНО проверок: %d" % len(fails))
        return 1
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
