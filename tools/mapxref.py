#!/usr/bin/env python3
"""
mapxref -- поиск карт и их осей по коду.

Идея. Обращение к калибровочной таблице в этой прошивке выглядит так:

    mov  r12, #0x529      ; указатель на тело карты   (файл 0x10529)
    mov  r13, #0x4b70     ; указатель на описание оси (файл 0x14B70)
    mov  r14, 0x9092      ; входная величина
    mov  r15, 0x9124      ; входная величина
    calls 0x0078b8        ; вызов процедуры интерполяции

То есть аргументы кладутся в r12..r15 непосредственными значениями, а затем
идёт вызов. Собрав все такие места, получаем:

  * какие подпрограммы являются процедурами интерполяции (к ним сходятся
    десятки разных указателей на карты);
  * полный список карт с их осями -- включая те, которых нет в DAMOS.

Оси лежат в формате "счётчик, затем точки": счётчик байтом (для байтовых
осей) либо словом (для 16-битных). Проверка -- строгая монотонность.

Использование:
    python3 tools/mapxref.py firmware/FBH3ID60_stok.bin --out out/xref.json
    python3 tools/mapxref.py firmware/FBH3ID60_stok.bin --callees
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict

import fwlib
import c166dis

# Калибровочный сегмент 0x81 -> файл 0x10000..0x1FFFF
DATA_BASE = 0x10000
DATA_END = 0x20000

# СТРАНИЦА. Половина калибровки этой прошивки лежит выше 0x18000, и
# указатель туда кладётся ПАРОЙ: смещение в r12, номер страницы в r13.
#
#     mov r12, #0x1636     ; KFLBTS
#     mov r13, #0x206      ; страница
#     mov r14, #0x1f7      ; ось SNM16GKUB
#     mov r15, #0x206
#     calls 0x833FFC
#
# Пока эта пара не разбиралась, весь топливный блок 0x19xxx был для
# поиска по коду невидим: смещение 0x1636 читалось как 0x11636, то есть
# как чужие байты. Отсюда и брались записи вида "ссылок из кода нет" у
# KRKTE и у кривых обогащения.
PAGES = {0x204: 0x10000, 0x205: 0x14000, 0x206: 0x18000, 0x207: 0x1C000}

# Мелкие непосредственные значения -- это счётчики, флаги и индексы, а не
# указатели. Без этого порога "картой" становится каждый mov rN, #0.
MIN_IMM = 0x80


def data_addr(imm: int, page: int | None = None) -> int | None:
    """Непосредственное 16-битное значение -> смещение в файле, если похоже на калибровку."""
    if page is not None:
        base = PAGES.get(page)
        if base is None:
            return None
        off = base + (imm & 0x3FFF)
        return off if DATA_BASE <= off < DATA_END else None
    if imm < MIN_IMM:
        return None
    off = DATA_BASE + imm
    return off if DATA_BASE <= off < DATA_END else None


def pointers(args: dict) -> list:
    """
    Аргументы вызова -> список указателей на калибровку.

    Возвращает пары (роль, адрес): роль "тело" для r12 и "ось"/"второй"
    для r14. Страница, если она есть, съедает соседний регистр -- иначе
    номер страницы 0x206 сам считался бы указателем на 0x10206.
    """
    out = []
    used = set()
    for lo, hi, role in ((12, 13, "тело"), (14, 15, "второй")):
        if lo not in args:
            continue
        page = args.get(hi)
        if page in PAGES:
            a = data_addr(args[lo], page)
            used.add(hi)
        else:
            a = data_addr(args[lo])
        if a is not None:
            out.append((role, a, args.get(hi) if hi in used else None))
            used.add(lo)
    # непарный r13 (ось без страницы) -- старая форма, встречается у
    # карт ниже 0x18000: mov r12,#0x529 / mov r13,#0x4b70
    if 13 in args and 13 not in used and 12 in used:
        a = data_addr(args[13])
        if a is not None:
            out.append(("ось", a, None))
    return out


def read_axis(fw: fwlib.Firmware, addr: int, max_n: int = 40):
    """
    Прочитать ось в формате "счётчик + точки".
    Пробуем счётчик байтом (точки u8) и словом (точки u16).
    Возвращает (разрядность, число точек, значения) либо None.
    """
    best = None
    for width, cnt_size in ((1, 1), (2, 2)):
        if not fw.in_range(addr, cnt_size):
            continue
        n = fw.u8(addr) if cnt_size == 1 else fw.u16(addr)
        if not (2 <= n <= max_n):
            continue
        vo = addr + cnt_size
        if not fw.in_range(vo, n * width):
            continue
        try:
            vals = fw.vec(vo, n, width)
        except Exception:
            continue
        if not fwlib.is_monotonic_increasing(vals, strict=True):
            continue
        if vals[-1] - vals[0] < n:
            continue
        cand = (width, n, vals, vo)
        # предпочитаем более длинную ось
        if best is None or n > best[1]:
            best = cand
    return best


class Scanner:
    def __init__(self, fw: fwlib.Firmware, code_start: int, code_end: int) -> None:
        self.fw = fw
        self.code_start = code_start
        self.code_end = code_end
        self.dis = c166dis.Disassembler()

    def scan(self, window: int = 12):
        """
        Линейно дизассемблируем код, копим непосредственные загрузки в r12..r15
        и при вызове фиксируем снимок аргументов.
        """
        data = self.fw.data
        sites = []
        regs: dict[int, tuple[int, int]] = {}   # rN -> (значение, адрес команды)
        ram: dict[int, tuple[int, int]] = {}    # rN -> (адрес ячейки-входа, адрес команды)
        off = self.code_start
        idx = 0
        while off < self.code_end:
            ins = self.dis.decode(data, off, off)
            if ins.length <= 0:
                break

            m = re.match(r'^r(\d+)$', ins.ops[0]) if ins.ops else None
            if ins.mnem == "mov" and m and len(ins.ops) == 2 \
                    and ins.ops[1].startswith("#"):
                try:
                    regs[int(m.group(1))] = (int(ins.ops[1][1:], 0), off)
                except ValueError:
                    pass
            elif ins.mnem in ("mov", "movb", "movbz", "movbs") and m \
                    and len(ins.ops) == 2 and re.match(r'^0x[0-9a-f]+$',
                                                       ins.ops[1]):
                # ВХОД, а не указатель: movbz r13, 0xf89e -- это обороты.
                # Без этого вход выглядел пустым местом, и карту нельзя
                # было отличить от карты с тем же телом, но другим входом.
                ram[int(m.group(1))] = (int(ins.ops[1], 0), off)
            elif ins.mnem in ("calls", "calla", "callr"):
                snap = {r: v for r, (v, a) in regs.items()
                        if r in (12, 13, 14, 15) and off - a <= window * 4}
                ins_snap = {r: v for r, (v, a) in ram.items()
                            if r in (12, 13, 14, 15) and off - a <= window * 4}
                if snap:
                    sites.append({
                        "site": off, "target": ins.ops[-1], "args": snap,
                        "inputs": ins_snap,
                        "dest": self._dest(data, off + ins.length),
                    })
                regs.clear()
                ram.clear()
            elif ins.mnem in ("ret", "rets", "reti", "retp"):
                regs.clear()
                ram.clear()

            off += ins.length
            idx += 1
        return sites

    def _dest(self, data, off, look: int = 3):
        """Куда лёг результат: ближайшее сохранение r4/RL4 в ячейку."""
        for _ in range(look):
            ins = self.dis.decode(data, off, off)
            if ins.length <= 0:
                return None
            if ins.mnem in ("mov", "movb") and len(ins.ops) == 2 \
                    and re.match(r'^0x[0-9a-f]+$', ins.ops[0]) \
                    and ins.ops[1] in ("r4", "RL4"):
                return int(ins.ops[0], 0)
            off += ins.length
        return None


def _auto_int(s: str) -> int:
    return int(s, 0)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Карты и оси по перекрёстным ссылкам кода")
    ap.add_argument("firmware")
    ap.add_argument("--code", nargs=2, type=_auto_int, default=[0x20000, 0x6C000],
                    metavar=("START", "END"))
    ap.add_argument("--min-refs", type=int, default=5,
                    help="сколько разных карт должно сходиться, чтобы счесть цель интерполятором")
    ap.add_argument("--callees", action="store_true", help="показать статистику по целям вызова")
    ap.add_argument("--names", help="JSON от xfer.py -- подписать известные имена")
    ap.add_argument("--axis-only", action="store_true",
                    help="только карты с подтверждённой осью")
    ap.add_argument("--out")
    args = ap.parse_args(argv)

    fw = fwlib.load(args.firmware)
    sc = Scanner(fw, args.code[0], args.code[1])
    print("Дизассемблирую 0x%X..0x%X ..." % tuple(args.code), file=sys.stderr)
    sites = sc.scan()
    print("Мест вызова с непосредственными аргументами: %d" % len(sites), file=sys.stderr)

    # какие цели получают указатели на калибровки
    by_target: dict[str, set] = defaultdict(set)
    for s in sites:
        for _role, a, _pg in pointers(s["args"]):
            by_target[s["target"]].add(a)

    ranked = sorted(by_target.items(), key=lambda kv: -len(kv[1]))
    if args.callees:
        print("\n%-12s %8s  %s" % ("ЦЕЛЬ", "КАРТ", "ПРИМЕРЫ"))
        for tgt, ptrs in ranked[:25]:
            ex = ", ".join("0x%05X" % p for p in sorted(ptrs)[:4])
            print("%-12s %8d  %s" % (tgt, len(ptrs), ex))

    interp = {t for t, p in ranked if len(p) >= args.min_refs}
    print("\nПодпрограмм-интерполяторов: %d" % len(interp), file=sys.stderr)

    # собрать записи карта+ось
    entries = []
    for s in sites:
        if s["target"] not in interp:
            continue
        ptrs = pointers(s["args"])
        if not ptrs or ptrs[0][0] != "тело":
            continue
        a12, page = ptrs[0][1], ptrs[0][2]
        a13 = next((a for role, a, _p in ptrs[1:]), None)
        ax = read_axis(fw, a13) if a13 is not None else None
        entries.append({
            "site": s["site"], "target": s["target"],
            "map_addr": a12, "page": page, "axis_addr": a13,
            "inputs": sorted(s.get("inputs", {}).values()),
            "dest": s.get("dest"),
            "axis_width": ax[0] if ax else None,
            "axis_n": ax[1] if ax else None,
            "axis_values": ax[2] if ax else None,
            "axis_data": ax[3] if ax else None,
        })

    # свернуть по карте
    per_map: dict[int, dict] = {}
    for e in entries:
        k = e["map_addr"]
        if k not in per_map or (e["axis_n"] and not per_map[k]["axis_n"]):
            per_map[k] = e

    names = {}
    if args.names:
        nd = json.load(open(args.names, encoding="utf-8"))
        for v in nd.get("variables", []):
            if v.get("addr") is not None:
                names.setdefault(v["addr"], v["name"])

    with_axis = sum(1 for e in per_map.values() if e["axis_n"])
    print("Разных карт по ссылкам из кода: %d (из них с подтверждённой осью: %d)"
          % (len(per_map), with_axis), file=sys.stderr)

    print("\n%-9s %-14s %-9s %-6s %-14s %-8s %s"
          % ("КАРТА", "ИМЯ", "ОСЬ", "ТОЧЕК", "ВХОДЫ", "РЕЗУЛЬТАТ", "ГДЕ В КОДЕ"))
    for k in sorted(per_map):
        e = per_map[k]
        if args.axis_only and not e["axis_n"]:
            continue
        ins = " ".join("0x%04X" % v for v in e.get("inputs") or []) or "-"
        print("0x%05X  %-14s %-9s %-6s %-14s %-8s 0x%06X" % (
            k, names.get(k, "")[:14],
            ("0x%05X" % e["axis_addr"]) if e["axis_addr"] else "-",
            e["axis_n"] or "-", ins,
            ("0x%04X" % e["dest"]) if e.get("dest") else "-",
            e["site"] + 0x800000))

    if args.out:
        for k, e in per_map.items():
            e["name"] = names.get(k)
        json.dump({"firmware": args.firmware,
                   "interpolators": sorted(interp),
                   "maps": [per_map[k] for k in sorted(per_map)]},
                  open(args.out, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
        print("\nЗаписано: %s" % args.out, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
