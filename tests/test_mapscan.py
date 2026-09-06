#!/usr/bin/env python3
"""Проверка mapscan на синтетическом образе с заранее известными картами."""

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import fwlib          # noqa: E402
import mapscan        # noqa: E402


def build_if_needed(outdir):
    if not os.path.exists(os.path.join(outdir, "synth_stock.bin")):
        subprocess.check_call([sys.executable,
                               os.path.join(ROOT, "tests", "make_synthetic.py"), outdir])


def main():
    outdir = os.path.join(ROOT, "out", "synth")
    build_if_needed(outdir)

    expected = json.load(open(os.path.join(outdir, "synth_expected.json")))["maps"]
    fw = fwlib.load(os.path.join(outdir, "synth_stock.bin"))

    sc = mapscan.Scanner(fw)
    lo = min(m["addr"] for m in expected) - 256
    hi = max(m["addr"] + m["size"] for m in expected) + 256
    found = mapscan.dedupe(sc.scan(lo, hi))
    by_addr = {c.addr: c for c in found}

    failures = []
    for m in expected:
        c = by_addr.get(m["addr"])
        if c is None:
            failures.append("НЕ НАЙДЕНА %s @ %s" % (m["name"], fwlib.hexa(m["addr"])))
            continue
        for key in ("kind", "nx", "ny", "axis_width", "data_width"):
            if getattr(c, key) != m[key]:
                failures.append("%s @ %s: %s ожидалось %r, получено %r" %
                                (m["name"], fwlib.hexa(m["addr"]), key, m[key], getattr(c, key)))

    extra = [c for c in found if c.addr not in {m["addr"] for m in expected}]

    print("Заложено карт : %d" % len(expected))
    print("Найдено       : %d" % len(found))
    print("Ложных        : %d" % len(extra))
    for c in extra:
        print("   лишняя: %s %s %dx%d оценка %.2f" %
              (fwlib.hexa(c.addr), c.kind, c.nx, c.ny, c.score))

    # --- проверка fwdiff: находит ли он карты зажигания ---------------
    import fwdiff
    a = fwlib.load(os.path.join(outdir, "synth_stock.bin"))
    b = fwlib.load(os.path.join(outdir, "synth_csok2.bin"))
    regions = fwdiff.diff_regions(a.data, b.data, merge_gap=16)
    ign = [m for m in expected if m.get("ignition")]
    print("\nИзменённых областей (csok1 vs csok2): %d, ожидалось карт зажигания: %d (+1 CRC)"
          % (len(regions), len(ign)))
    for m in ign:
        do = m["addr"] + m["data_off"]
        hit = any(r.start <= do < r.end for r in regions)
        print("   %-14s данные @ %s : %s" %
              (m["name"], fwlib.hexa(do), "покрыта" if hit else "ПРОПУЩЕНА"))
        if not hit:
            failures.append("fwdiff не покрыл %s" % m["name"])

    print()
    if failures:
        for f in failures:
            print("ОШИБКА:", f)
        return 1
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
