#!/usr/bin/env python3
"""
reanchor -- переякоривание разметки DAMOS по ссылкам кода.

Метод тот, которым нашлась группа отсечки топлива, только не руками.

Наблюдение: подряд идущие скаляры DAMOS лежат в прошивке подряд же, и
целый такой участок читается кодом ячейка в ячейку. Значит если взять
цепочку из N соседних скаляров и подобрать сдвиг, при котором ВСЕ N
попадают в список ячеек, которые код действительно читает, -- это и есть
адрес участка в целевой прошивке.

Почему это работает. Кодом читается около 1665 ячеек из 65536, то есть
случайное попадание одной ячейки -- примерно 2.5 %. Цепочка из пятнадцати
подряд даёт 2.5% ^ 15, совпасть случайно она не может.

Список читаемых ячеек берётся из tools/calref.py.

    python3 tools/calref.py firmware/FBH3ID60_stok.bin --out out/calref.json
    python3 tools/reanchor.py --damos firmware/px5ns03d.dam --refs out/calref.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict

import damos

FLASH_BASE = 0x800000
DATA_LO, DATA_HI = 0x10000, 0x20000


def scalar_runs(d: damos.Damos, max_gap: int) -> list[list[tuple[int, str]]]:
    """Цепочки подряд идущих скаляров по исходным адресам."""
    sc = sorted(((v.addr - FLASH_BASE, v.name) for v in d.variables
                 if v.nx == 0 and v.ny == 0 and DATA_LO <= v.addr - FLASH_BASE < DATA_HI),
                key=lambda t: t[0])
    runs, cur = [], []
    for item in sc:
        if cur and item[0] - cur[-1][0] > max_gap:
            runs.append(cur)
            cur = []
        cur.append(item)
    if cur:
        runs.append(cur)
    return runs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Переякоривание DAMOS по ссылкам кода")
    ap.add_argument("--damos", required=True)
    ap.add_argument("--refs", required=True, help="JSON от calref.py")
    ap.add_argument("--min-len", type=int, default=8, help="минимальная длина цепочки")
    ap.add_argument("--min-rate", type=float, default=0.85, help="минимальная доля попаданий")
    ap.add_argument("--shift", type=int, default=0x600, help="полуширина перебора сдвигов")
    ap.add_argument("--max-gap", type=int, default=4, help="макс. разрыв внутри цепочки, байт")
    ap.add_argument("--unique-only", action="store_true",
                    help="только однозначные попадания")
    ap.add_argument("--out")
    a = ap.parse_args(argv)

    d = damos.parse(a.damos)
    refs = {int(k, 16) for k in json.load(open(a.refs))}
    runs = scalar_runs(d, a.max_gap)
    print(f"цепочек скаляров в DAMOS: {len(runs)}, "
          f"из них длиннее {a.min_len}: {sum(1 for r in runs if len(r) >= a.min_len)}",
          file=sys.stderr)
    print(f"ячеек, читаемых кодом: {len(refs)}", file=sys.stderr)

    found = []
    for run in runs:
        if len(run) < a.min_len:
            continue
        addrs = [x for x, _ in run]
        base = addrs[0]
        best = []
        for delta in range(-a.shift, a.shift + 1):
            hits = sum(1 for x in addrs if x + delta in refs)
            if hits / len(addrs) >= a.min_rate:
                best.append((hits, delta))
        if not best:
            continue
        best.sort(key=lambda t: (-t[0], abs(t[1])))
        top = best[0]
        # неоднозначность: несколько сдвигов с тем же счётом
        ties = [dl for h, dl in best if h == top[0]]
        found.append(dict(start=base, length=len(run), hits=top[0], delta=top[1],
                          ambiguous=len(ties) > 1, ties=ties[:5],
                          first=run[0][1], last=run[-1][1],
                          names=[n for _, n in run]))

    found.sort(key=lambda f: -f["hits"])
    if a.unique_only:
        found = [f for f in found if not f["ambiguous"]]
    for f in found:
        mark = "  НЕОДНОЗНАЧНО" if f["ambiguous"] else ""
        print(f"0x{f['start']:05X} +{f['delta']:<5d} -> 0x{f['start'] + f['delta']:05X}  "
              f"{f['hits']:3d}/{f['length']:<3d}  {f['first']}..{f['last']}{mark}")
    if a.out:
        json.dump(found, open(a.out, "w"), ensure_ascii=False, indent=1)
        print(f"записано {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
