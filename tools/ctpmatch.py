#!/usr/bin/env python3
"""
ctpmatch -- сопоставить карты CTP7 с нашими по правкам.

Владелец купил ChipTuningPRO 7: карт там мало, но каждая -- независимый
источник, не связанный ни с дамосом, ни с моим выравниванием. Способ
проверки простой и окончательный: в CTP7 правится ОДНА карта, файл
сохраняется под именем этой карты, и дифф со стоком показывает, по каким
байтам она лежит на самом деле.

Порядок для владельца:

1. открыть в CTP7 стоковый FBH3ID60;
2. выбрать одну карту, поднять ВСЮ карту на одну ступень (все ячейки), --
   так дифф покажет не только адрес, но и ГРАНИЦЫ карты;
3. сохранить файл под именем карты, как его пишет CTP7, например
   "Угол опережения зажигания.bin";
4. вернуться к стоку и повторить для следующей карты;
5. сложить файлы в одну папку и прислать.

    python3 tools/ctpmatch.py --stock firmware/FBH3ID60_stok.bin \\
        --a2l results/FBH3ID60_legacy.a2l firmware/ctp7/*.bin

Приговоры:

* СОВПАЛО ЦЕЛИКОМ -- правка покрыла ровно нашу карту, от первого байта до
  последнего; адрес и размер подтверждены;
* ВНУТРИ НАШЕЙ КАРТЫ -- адрес верен, но правка заняла не всю карту
  (ячейки на пределе не двигаются, или CTP7 знает карту меньше);
* ВЫХОДИТ ЗА НАШУ КАРТУ -- у нас неверный размер или граница;
* НЕСКОЛЬКО НАШИХ КАРТ -- у CTP7 одна карта там, где у нас несколько:
  кто-то из нас ошибся в делении;
* ВНЕ НАШИХ КАРТ -- карта, которой у нас нет вовсе.

Таблица контрольных сумм и область идентификации из диффа исключаются:
CTP7 пересчитывает суммы сам, и эти байты о картах ничего не говорят.
"""

from __future__ import annotations

import argparse
import bisect
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "editor", "a2l"))
sys.path.insert(0, os.path.join(ROOT, "editor", "core"))

import geometry                                             # noqa: E402
import model                                                # noqa: E402

SKIP = ((0x10000, 0x10030), (0x1FC00, 0x1FD00))


def regions(addrs, gap: int = 2):
    """Подряд идущие изменённые байты -> участки (с допуском в gap байт)."""
    out = []
    for a in sorted(addrs):
        if out and a - out[-1][1] <= gap:
            out[-1][1] = a
        else:
            out.append([a, a])
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Сопоставить карты CTP7 с нашими")
    ap.add_argument("--stock", required=True)
    ap.add_argument("--a2l", default=os.path.join(ROOT, "results",
                                                   "FBH3ID60_legacy.a2l"))
    ap.add_argument("files", nargs="+", help="файлы из CTP7, имя = имя карты")
    ap.add_argument("--json")
    a = ap.parse_args(argv)

    stock = open(a.stock, "rb").read()
    a2l = model.load(a.a2l)
    am = geometry.detect_addressing(a2l, len(stock))
    lay = geometry.resolve_all(a2l, stock, am)
    spans = sorted((L.data_off, L.end, n) for n, L in lay.items() if L.size)
    starts = [s[0] for s in spans]
    widest = max(e - s for s, e, _ in spans)
    ru = {}
    try:
        ru = json.load(open(os.path.splitext(a.a2l)[0] + ".ru.json",
                            encoding="utf-8"))
    except Exception:                                       # noqa: BLE001
        pass
    check = {}
    try:
        check = json.load(open(os.path.splitext(a.a2l)[0] + ".check.json",
                               encoding="utf-8"))
    except Exception:                                       # noqa: BLE001
        pass

    def owners(lo, hi):
        found = set()
        i = bisect.bisect_right(starts, hi) - 1
        while i >= 0 and spans[i][0] > lo - widest:
            s, e, n = spans[i]
            if s <= hi and lo < e:
                found.add(n)
            i -= 1
        return sorted(found, key=lambda n: lay[n].data_off)

    report = []
    for path in a.files:
        data = open(path, "rb").read()
        label = os.path.splitext(os.path.basename(path))[0]
        if len(data) != len(stock):
            print("%s: размер %d, а у стока %d -- не та прошивка"
                  % (label, len(data), len(stock)))
            continue
        ch = [x for x in range(0x10000, 0x20000) if data[x] != stock[x]
              and not any(lo <= x < hi for lo, hi in SKIP)]
        if not ch:
            print("\n%s: калибровка не изменилась" % label)
            continue
        print("\n== %s: изменено байт %d" % (label, len(ch)))
        for lo, hi in regions(ch):
            own = owners(lo, hi)
            n_ch = sum(1 for x in ch if lo <= x <= hi)
            if not own:
                verdict = "ВНЕ НАШИХ КАРТ -- такой карты у нас нет"
            elif len(own) > 1:
                verdict = "НЕСКОЛЬКО НАШИХ КАРТ: " + ", ".join(own)
            else:
                L = lay[own[0]]
                if lo < L.data_off or hi >= L.end:
                    verdict = "ВЫХОДИТ ЗА НАШУ КАРТУ %s (у нас 0x%05X..0x%05X)" \
                        % (own[0], L.data_off, L.end - 1)
                else:
                    # По ЯЧЕЙКАМ, а не по байтам: у словной карты правка на
                    # одну ступень обычно меняет только младший байт, и
                    # последний изменённый байт стоит за байт до конца.
                    w = max(1, L.width)
                    c0 = (lo - L.data_off) // w
                    c1 = (hi - L.data_off) // w
                    cells = (L.end - L.data_off) // w
                    if c0 == 0 and c1 == cells - 1:
                        verdict = "СОВПАЛО ЦЕЛИКОМ: %s" % own[0]
                    else:
                        verdict = ("ВНУТРИ НАШЕЙ КАРТЫ %s (ячейки %d..%d из %d)"
                                   % (own[0], c0, c1, cells))
            print("   0x%05X..0x%05X (%d б)  %s" % (lo, hi, n_ch, verdict))
            for n in own[:3]:
                c = check.get(n, {}).get("verdict", "")
                print("        %s -- %s%s" % (n, ru.get(n, ""),
                                             ("; у нас: " + c) if c else ""))
            report.append({"ctp7": label, "from": "0x%05X" % lo,
                           "to": "0x%05X" % hi, "bytes": n_ch,
                           "ours": own, "verdict": verdict})
    if a.json:
        json.dump(report, open(a.json, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
