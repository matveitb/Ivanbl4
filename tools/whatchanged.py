#!/usr/bin/env python3
"""
whatchanged -- что именно правил калибровщик, с именами и в физических единицах.

Берёт дифф двух прошивок и накладывает его на итоговую разметку
(out/FBH3ID60_maps.json из tools/report.py). Для каждой изменённой области
находит карту, которая её покрывает, и печатает изменения в физических
единицах, а не в сырых байтах.

Использование:
    python3 tools/whatchanged.py --maps out/FBH3ID60_maps.json \\
        firmware/FBH3ID60_stok.bin "firmware/FBH3ID60 e2 tun csok.bin" \\
        --md out/ЧТО_ИЗМЕНЕНО.md
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import fwlib
import fwdiff


def load_maps(path: str) -> list[dict]:
    d = json.load(open(path, encoding="utf-8"))
    ms = [m for m in d.get("maps", []) if m.get("addr") is not None]
    for m in ms:
        dw = m.get("data_width", 1)
        m["_lo"] = m.get("data_addr", m["addr"])
        m["_hi"] = m["_lo"] + m["nx"] * m["ny"] * dw
    return sorted(ms, key=lambda m: m["_lo"])


def covering(maps: list[dict], lo: int, hi: int) -> list[dict]:
    return [m for m in maps if m["_lo"] < hi and lo < m["_hi"]]


def cells(fw: fwlib.Firmware, m: dict) -> list[int]:
    dw = m.get("data_width", 1)
    return fw.vec(m["_lo"], m["nx"] * m["ny"], dw, signed=bool(m.get("signed")))


def phys(v: float, m: dict) -> float:
    return v * m.get("factor", 1.0) - m.get("shift", 0.0)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Что изменил калибровщик")
    ap.add_argument("file_a")
    ap.add_argument("file_b")
    ap.add_argument("--maps", required=True)
    ap.add_argument("--merge-gap", type=int, default=16)
    ap.add_argument("--md")
    ap.add_argument("--min-confidence", default="",
                    help="показывать только карты с этой достоверностью")
    args = ap.parse_args(argv)

    a = fwlib.load(args.file_a)
    b = fwlib.load(args.file_b)
    maps = load_maps(args.maps)

    regions = fwdiff.diff_regions(a.data, b.data, merge_gap=args.merge_gap)
    fwdiff.classify(regions)

    rows = []
    unnamed = []
    for r in regions:
        cov = covering(maps, r.start, r.end)
        if not cov:
            unnamed.append(r)
            continue
        for m in cov:
            fa, fb = cells(a, m), cells(b, m)
            diffs = [(i, x, y) for i, (x, y) in enumerate(zip(fa, fb)) if x != y]
            if not diffs:
                continue
            deltas = [y - x for _, x, y in diffs]
            rows.append({
                "map": m, "changed": len(diffs), "total": len(fa),
                "dmin": min(deltas), "dmax": max(deltas),
                "pa": (min(fa), max(fa)), "pb": (min(fb), max(fb)),
            })

    # убрать дубли (одна карта могла попасть из нескольких областей)
    seen = {}
    for row in rows:
        key = row["map"]["_lo"]
        if key not in seen or row["changed"] > seen[key]["changed"]:
            seen[key] = row
    rows = sorted(seen.values(), key=lambda r: -r["changed"])

    total_changed = sum(r.changed_bytes for r in regions)
    print("A: %s\nB: %s" % (os.path.basename(args.file_a), os.path.basename(args.file_b)))
    print("Изменено байт: %d, областей: %d, опознано карт: %d\n"
          % (total_changed, len(regions), len(rows)))

    print("%-13s %-9s %-8s %9s  %s" % ("КАРТА", "АДРЕС", "РАЗМЕР", "ЯЧЕЕК", "ИЗМЕНЕНИЕ"))
    for row in rows:
        m = row["map"]
        f = m.get("factor", 1.0)
        unit = m.get("unit", "") or ""
        d1, d2 = row["dmin"] * f, row["dmax"] * f
        print("%-13s 0x%05X   %-8s %4d/%-4d  %+.3g..%+.3g %s   [%s]" % (
            m["name"][:13], m["_lo"], "%dx%d" % (m["nx"], m["ny"]),
            row["changed"], row["total"], d1, d2, unit,
            m.get("confidence", "")))

    if unnamed:
        print("\nОбласти без опознанной карты (%d):" % len(unnamed))
        for r in unnamed:
            tag = (" -- таблица контрольных сумм" if r.kind == "checksum"
                   else " -- мелкая правка: скаляр или порог" if r.kind == "small" else "")
            print("   0x%05X..0x%05X  %d байт%s" % (r.start, r.end, r.size, tag))

    if args.md:
        with open(args.md, "w", encoding="utf-8") as fh:
            fh.write("# Что изменил калибровщик\n\n")
            fh.write("- Исходная прошивка: `%s`\n" % os.path.basename(args.file_a))
            fh.write("- Изменённая прошивка: `%s`\n" % os.path.basename(args.file_b))
            fh.write("- Изменено байт: **%d**, областей: **%d**, "
                     "опознано карт: **%d**\n\n" % (total_changed, len(regions), len(rows)))
            fh.write("## Изменённые карты\n\n")
            fh.write("| Карта | Адрес | Размер | Ячеек | Изменение | Ед. | "
                     "Было | Стало | Достоверность | Описание |\n")
            fh.write("|---|---|---|---|---|---|---|---|---|---|\n")
            for row in rows:
                m = row["map"]
                f = m.get("factor", 1.0)
                fh.write("| `%s` | `0x%05X` | %dx%d | %d/%d | %+.3g..%+.3g | %s | "
                         "%.4g..%.4g | %.4g..%.4g | %s | %s |\n" % (
                    m["name"], m["_lo"], m["nx"], m["ny"],
                    row["changed"], row["total"],
                    row["dmin"] * f, row["dmax"] * f, m.get("unit", "") or "-",
                    phys(row["pa"][0], m), phys(row["pa"][1], m),
                    phys(row["pb"][0], m), phys(row["pb"][1], m),
                    m.get("confidence", ""), (m.get("note") or "")[:80]))
            if unnamed:
                fh.write("\n## Области без опознанной карты\n\n")
                fh.write("| Начало | Конец | Байт | Замечание |\n|---|---|---|---|\n")
                for r in unnamed:
                    tag = ("таблица контрольных сумм" if r.kind == "checksum"
                           else "мелкая правка: скаляр или порог" if r.kind == "small" else "")
                    fh.write("| `0x%05X` | `0x%05X` | %d | %s |\n"
                             % (r.start, r.end, r.size, tag))
            fh.write("\n> Строки с достоверностью «вероятная» или «сомнительная» "
                     "получены переносом из DAMOS родственной прошивки — "
                     "адрес может быть смещён. Проверяйте в WinOLS.\n")
        print("\nЗаписано: %s" % args.md, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
