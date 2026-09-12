#!/usr/bin/env python3
"""
blockfind -- поиск блоков скаляров DAMOS в целевой прошивке.

Метод тот, которым нашлись отсечка топлива и контур детонации.

Подряд идущие скаляры DAMOS лежат в прошивке подряд же. Берём такую
цепочку и двигаем её по всему калибровочному сегменту, проверяя два
условия сразу:

  1. ВСЕ значения цепочки попадают в свои допустимые диапазоны,
     объявленные в DAMOS (w_min..w_max после пересчёта);
  2. достаточная доля ячеек цепочки реально читается кодом
     (список из tools/calref.py).

По отдельности каждое условие слабое, вместе -- очень жёсткое. Совпадение
принимается, только если база единственная: две и более -- отбрасываем.

Дополнительно считается, сколько значений совпало с исходной прошивкой,
к которой относится DAMOS. Это независимое подтверждение: родственные
калибровки многие константы не меняют.

    python3 tools/calref.py firmware/FBH3ID60_stok.bin --out out/calref.json
    python3 tools/blockfind.py --damos firmware/px5ns03d.dam \\
        --source out/px5ns03d_flash.bin --target firmware/FBH3ID60_stok.bin \\
        --refs out/calref.json --out out/blocks.json
"""

from __future__ import annotations

import argparse
import json
import sys

import damos

FLASH_BASE = 0x800000
DATA_LO, DATA_HI = 0x10000, 0x20000


def conv_of(d: damos.Damos, v: damos.Variable):
    c = d.conv(v.conv_w)
    f = c.factor if c else 1.0
    sh = c.shift if c else 0.0
    w = 2 if (c and c.raw_max >= 65535) else 1
    return f, sh, w


def chains(d: damos.Damos, max_gap: int, min_len: int):
    sc = sorted((v for v in d.variables
                 if v.nx == 0 and v.ny == 0
                 and DATA_LO <= v.addr - FLASH_BASE < DATA_HI),
                key=lambda v: v.addr)
    out, cur = [], []
    for v in sc:
        if cur and (v.addr - cur[-1].addr) > max_gap:
            if len(cur) >= min_len:
                out.append(cur)
            cur = []
        cur.append(v)
    if len(cur) >= min_len:
        out.append(cur)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Поиск блоков скаляров DAMOS по допускам и ссылкам кода")
    ap.add_argument("--damos", required=True)
    ap.add_argument("--source", required=True, help="образ, к которому относится DAMOS")
    ap.add_argument("--target", required=True)
    ap.add_argument("--refs", required=True, help="JSON от calref.py")
    ap.add_argument("--min-len", type=int, default=8)
    ap.add_argument("--max-gap", type=int, default=4)
    ap.add_argument("--min-read", type=float, default=0.5, help="доля ячеек, читаемых кодом")
    ap.add_argument("--out")
    a = ap.parse_args(argv)

    d = damos.parse(a.damos)
    src = open(a.source, "rb").read()
    tgt = open(a.target, "rb").read()
    read = {int(k, 16) for k in json.load(open(a.refs))}
    u16 = lambda b, x: b[x] | (b[x + 1] << 8)

    found = []
    for chain in chains(d, a.max_gap, a.min_len):
        base_src = chain[0].addr - FLASH_BASE
        spec = []
        for v in chain:
            f, sh, w = conv_of(d, v)
            spec.append((v.addr - FLASH_BASE - base_src, v, f, sh, w))
        span = spec[-1][0] + spec[-1][4]
        hits = []
        for base in range(DATA_LO, DATA_HI - span):
            ok = True
            rd = 0
            ex = 0
            for off, v, f, sh, w in spec:
                t = base + off
                raw = tgt[t] if w == 1 else u16(tgt, t)
                val = raw * f - sh
                if not (v.w_min - 1e-9 <= val <= v.w_max + 1e-9):
                    ok = False
                    break
                if t in read:
                    rd += 1
                s = base_src + off
                sraw = src[s] if w == 1 else u16(src, s)
                if abs(val - (sraw * f - sh)) < 1e-9:
                    ex += 1
            if ok and rd >= len(spec) * a.min_read:
                hits.append((base, rd, ex))
        if len(hits) != 1:
            continue
        base, rd, ex = hits[0]
        found.append(dict(src=f"0x{base_src:05X}", base=f"0x{base:05X}", delta=base - base_src,
                          length=len(spec), read=rd, exact=ex,
                          first=chain[0].name, last=chain[-1].name,
                          names=[v.name for v in chain]))

    found.sort(key=lambda x: -x["length"])
    for f in found:
        print(f"{f['src']} -> {f['base']}  сдвиг {f['delta']:+6d}  "
              f"{f['length']:3d} шт, читается {f['read']}, совпало с исходником {f['exact']}  "
              f"{f['first']}..{f['last']}")
    print(f"\nоднозначных блоков: {len(found)}, "
          f"переменных в них: {sum(f['length'] for f in found)}", file=sys.stderr)
    if a.out:
        json.dump(found, open(a.out, "w"), ensure_ascii=False, indent=1)
        print(f"записано {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
