#!/usr/bin/env python3
"""
axisscan -- поиск осей в прошивке.

Ищет два вида:

  * С ПРЕФИКСОМ ДЛИНЫ:  n:u8, X[n]   -- байт длины, затем n строго
    возрастающих значений. Именно так оси лежат в FBH3ID60.
  * ГОЛЫЕ: просто монотонно возрастающий прогон нужной длины (для случаев,
    когда длина хранится где-то в коде).

Найденные оси нужны, чтобы привязать к таблицам из gridscan.py: у карты
шириной W ось X должна иметь ровно W точек, у карты высотой H ось Y -- H точек.

Использование:
    python3 tools/axisscan.py firmware/stock.bin --range 0x10000 0x1C000
    python3 tools/axisscan.py firmware/stock.bin --len 11 12 16
    python3 tools/axisscan.py firmware/stock.bin --near 0x10529 --window 0x400
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, asdict

import fwlib
from fwlib import Firmware


@dataclass
class Axis:
    addr: int          # адрес байта длины (или первого значения, если голая)
    values_addr: int
    count: int
    width: int         # 1 или 2 байта на точку
    prefixed: bool
    values: list
    vmin: int = 0
    vmax: int = 0
    refs: int = 0

    def end(self) -> int:
        return self.values_addr + self.count * self.width


def scan_prefixed(fw: Firmware, start: int, end: int, lengths: set[int] | None,
                  widths=(1, 2), min_len=4, max_len=32) -> list[Axis]:
    out = []
    data = fw.data
    for addr in range(start, min(end, len(fw) - 2)):
        n = data[addr]
        if not (min_len <= n <= max_len):
            continue
        if lengths and n not in lengths:
            continue
        for w in widths:
            vo = addr + 1
            if vo + n * w > len(fw):
                continue
            try:
                vals = fw.vec(vo, n, w)
            except IndexError:
                continue
            if not fwlib.is_monotonic_increasing(vals, strict=True):
                continue
            if vals[-1] - vals[0] < n:      # слишком плоско -- вряд ли ось
                continue
            out.append(Axis(addr=addr, values_addr=vo, count=n, width=w,
                            prefixed=True, values=vals,
                            vmin=vals[0], vmax=vals[-1]))
    return out


def scan_bare(fw: Firmware, start: int, end: int, lengths: set[int],
              widths=(1, 2)) -> list[Axis]:
    out = []
    for w in widths:
        for n in sorted(lengths):
            step = w
            addr = start
            while addr + n * w <= min(end, len(fw)):
                try:
                    vals = fw.vec(addr, n, w)
                except IndexError:
                    break
                if (fwlib.is_monotonic_increasing(vals, strict=True)
                        and vals[-1] - vals[0] >= n):
                    out.append(Axis(addr=addr, values_addr=addr, count=n,
                                    width=w, prefixed=False, values=vals,
                                    vmin=vals[0], vmax=vals[-1]))
                addr += step
    return out


def scan_chains(fw: Firmware, start: int, end: int, min_links: int = 3,
                min_len: int = 4, max_len: int = 32) -> list[list[Axis]]:
    """
    Цепочечный разбор: оси в M7.9.7 лежат подряд, вплотную друг к другу
    (n, X[n], n, X[n], ...). Если с некоторого адреса подряд читается
    несколько корректных осей, это снимает неоднозначность одиночного
    поиска -- ложный "байт длины" внутри чужой оси цепочку не продолжит.

    Возвращает список цепочек длиной не менее min_links.
    """
    chains: list[list[Axis]] = []
    consumed: set[int] = set()

    for begin in range(start, min(end, len(fw))):
        if begin in consumed:
            continue
        off = begin
        links: list[Axis] = []
        while off < end:
            n = fw.u8(off)
            if not (min_len <= n <= max_len) or off + 1 + n > len(fw):
                break
            vals = fw.vec(off + 1, n, 1)
            if not fwlib.is_monotonic_increasing(vals, strict=True):
                break
            if vals[-1] - vals[0] < n:
                break
            links.append(Axis(addr=off, values_addr=off + 1, count=n, width=1,
                              prefixed=True, values=vals,
                              vmin=vals[0], vmax=vals[-1]))
            off += 1 + n
        if len(links) >= min_links:
            chains.append(links)
            for a in links:
                for x in range(a.addr, a.end()):
                    consumed.add(x)
    return chains


def dedupe(axes: list[Axis]) -> list[Axis]:
    """Оставить самые длинные оси, поглощая вложенные."""
    ranked = sorted(axes, key=lambda a: (-a.count, a.addr))
    taken: list[Axis] = []
    for a in ranked:
        lo, hi = a.values_addr, a.end()
        if any(lo >= t.values_addr and hi <= t.end() and t.width == a.width
               for t in taken):
            continue
        taken.append(a)
    return sorted(taken, key=lambda a: a.addr)


def _auto_int(s: str) -> int:
    return int(s, 0)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Поиск осей")
    ap.add_argument("firmware")
    ap.add_argument("--range", nargs=2, type=_auto_int, metavar=("START", "END"))
    ap.add_argument("--near", type=_auto_int, help="искать вокруг адреса")
    ap.add_argument("--window", type=_auto_int, default=0x400)
    ap.add_argument("--len", type=int, nargs="*", dest="lengths",
                    help="искать оси только этих длин")
    ap.add_argument("--bare", action="store_true", help="искать и оси без префикса длины")
    ap.add_argument("--min-len", type=int, default=4)
    ap.add_argument("--max-len", type=int, default=32)
    ap.add_argument("--width", type=int, nargs="*", default=[1, 2])
    ap.add_argument("--refs", action="store_true", help="считать xref (медленнее)")
    ap.add_argument("--chains", action="store_true",
                    help="цепочечный разбор: искать блоки идущих подряд осей")
    ap.add_argument("--min-links", type=int, default=3,
                    help="минимум осей подряд, чтобы принять цепочку")
    ap.add_argument("--out")
    args = ap.parse_args(argv)

    fw = fwlib.load(args.firmware)
    if args.near is not None:
        start = max(0, args.near - args.window)
        end = min(len(fw), args.near + args.window)
    elif args.range:
        start, end = args.range
    else:
        start, end = 0, len(fw)

    lengths = set(args.lengths) if args.lengths else None
    widths = tuple(args.width)

    if args.chains:
        chains = scan_chains(fw, start, end, args.min_links,
                             args.min_len, args.max_len)
        total = sum(len(c) for c in chains)
        print("Цепочек: %d, осей в них: %d\n" % (len(chains), total), file=sys.stderr)
        for c in chains:
            print("--- цепочка %s..%s (%d осей) ---" %
                  (fwlib.hexa(c[0].addr), fwlib.hexa(c[-1].end()), len(c)))
            for a in c:
                vals = a.values if len(a.values) <= 18 else a.values[:17] + ["..."]
                print("  %s  n=%-3d %s" % (fwlib.hexa(a.addr), a.count,
                                           " ".join(str(v) for v in vals)))
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump({"firmware": args.firmware,
                           "chains": [[asdict(a) for a in c] for c in chains]},
                          fh, indent=1, ensure_ascii=False)
            print("\nЗаписано: %s" % args.out, file=sys.stderr)
        return 0

    axes = scan_prefixed(fw, start, end, lengths, widths,
                         args.min_len, args.max_len)
    if args.bare and lengths:
        axes += scan_bare(fw, start, end, lengths, widths)
    axes = dedupe(axes)

    if args.refs:
        for a in axes:
            a.refs = len(fwlib.find_le16_refs(fw, a.addr, limit=32))

    print("Диапазон %s..%s, найдено осей: %d\n" %
          (fwlib.hexa(start), fwlib.hexa(end), len(axes)), file=sys.stderr)
    print("%-10s %-10s %5s %5s %5s %5s  %s" %
          ("АДРЕС", "ЗНАЧЕНИЯ", "ТОЧЕК", "РАЗР", "ПРЕФ", "XREF", "ЗНАЧЕНИЯ"))
    for a in axes:
        vals = a.values if len(a.values) <= 18 else a.values[:17] + ["..."]
        print("%-10s %-10s %5d %5s %5s %5d  %s" % (
            fwlib.hexa(a.addr), fwlib.hexa(a.values_addr), a.count,
            "u%d" % (a.width * 8), "да" if a.prefixed else "нет", a.refs,
            " ".join(str(v) for v in vals)))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"firmware": args.firmware,
                       "axes": [asdict(a) for a in axes]}, fh,
                      indent=1, ensure_ascii=False)
        print("\nЗаписано: %s" % args.out, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
