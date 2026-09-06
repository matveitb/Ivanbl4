#!/usr/bin/env python3
"""
xfer -- перенос разметки из DAMOS одной прошивки на другую прошивку того же
семейства (здесь: px5ns03d / M7.9 -> FBH3ID60 / M7.9.7).

Прошивки родственные: тот же порядок калибровочных структур, но размеры
таблиц местами отличаются, поэтому адреса плывут. Поэтому работаем в два шага.

ШАГ 1. ЯКОРЯ. Для записей, у которых есть проверяемая структурная подпись,
ищем точное совпадение в целевой прошивке в окне вокруг исходного адреса:

    тип 3  -- заголовок: байт nx (строки), байт ny (столбцы), затем строго
              возрастающие оси X[nx] и Y[ny];
    тип 6  -- ось: байт длины n, затем n строго возрастающих значений;
    тип 2  -- кривая: байт длины n.

Совпадение принимается только если оно ЕДИНСТВЕННОЕ в окне -- иначе якорь
неоднозначен и отбрасывается.

ШАГ 2. ВЫРАВНИВАНИЕ. Якоря сортируются по исходному адресу и задают кусочную
функцию сдвига. Все прочие записи переносятся через неё, с пометкой, что
адрес получен выравниванием, а не найден структурно.

Использование:
    python3 tools/xfer.py --damos firmware/px5ns03d.dam \\
        --source out/px5ns03d_flash.bin --target firmware/FBH3ID60_stok.bin \\
        --out out/xfer_FBH3ID60.json
"""

from __future__ import annotations

import argparse
import bisect
import json
import sys

import fwlib
import damos
import gridscan
from fwlib import Firmware


# --------------------------------------------------------------------------
# Разрядность по пересчёту
# --------------------------------------------------------------------------

def conv_width(d: damos.Damos, cid: int) -> int:
    c = d.conv(cid)
    if not c:
        return 1
    if c.raw_max >= 65535:
        return 2
    return 1


# --------------------------------------------------------------------------
# Структурные проверки
# --------------------------------------------------------------------------

def check_axis(fw: Firmware, addr: int, n: int, w: int) -> bool:
    if addr < 0 or addr + 1 + n * w > len(fw):
        return False
    if fw.u8(addr) != n:
        return False
    try:
        vals = fw.vec(addr + 1, n, w)
    except Exception:
        return False
    return fwlib.is_monotonic_increasing(vals, strict=True)


def check_map3(fw: Firmware, addr: int, w: int, h: int, aw: int) -> bool:
    """Заголовок: nx=h (строки), ny=w (столбцы), затем оси X[h] и Y[w]."""
    if addr < 0 or addr + 2 + (h + w) * aw > len(fw):
        return False
    if fw.u8(addr) != h or fw.u8(addr + 1) != w:
        return False
    try:
        xs = fw.vec(addr + 2, h, aw)
        ys = fw.vec(addr + 2 + h * aw, w, aw)
    except Exception:
        return False
    return (fwlib.is_monotonic_increasing(xs, strict=True)
            and fwlib.is_monotonic_increasing(ys, strict=True))


def check_count(fw: Firmware, addr: int, n: int) -> bool:
    return 0 <= addr < len(fw) and fw.u8(addr) == n


def signature(d: damos.Damos, v: damos.Variable):
    """Вернуть функцию проверки (fw, addr) -> bool, либо None."""
    w, h = d.dims(v)
    if v.typ == 3 and w > 1 and h > 1:
        aw = conv_width(d, v.conv_x)
        return lambda fw, a: check_map3(fw, a, w, h, aw)
    if v.typ == 6 and w > 1:
        aw = conv_width(d, v.conv_x) or 1
        return lambda fw, a: check_axis(fw, a, w, aw)
    if v.typ == 2 and w > 1:
        return lambda fw, a: check_count(fw, a, w)
    return None


def data_offset(d: damos.Damos, v: damos.Variable, addr: int) -> int:
    """Адрес начала тела карты (для типа 3 -- после заголовка и осей)."""
    w, h = d.dims(v)
    if v.typ == 3:
        aw = conv_width(d, v.conv_x)
        return addr + 2 + (h + w) * aw
    if v.typ == 2:
        return addr + 1
    return addr


def verify_grid(fw: Firmware, data_addr: int, w: int, h: int, dw: int) -> str:
    """
    Независимая проверка тела карты: детектор ширины из gridscan должен
    выдать ту же ширину, что обещает DAMOS.

    Возвращает "подтверждена" / "не сходится" / "нечего проверять".
    """
    if dw != 1 or w < 4 or h < 3:
        return "нечего проверять"
    size = w * h
    if data_addr < 0 or data_addr + size > len(fw):
        return "нечего проверять"
    blk = fw.data[data_addr:data_addr + size]
    if fwlib.is_blank(blk) or len(set(blk)) <= 2:
        return "нечего проверять"
    probe = min(size, max(48, w * 6))
    got, score, rival = gridscan.best_width(fw.data, data_addr, probe,
                                            max(3, w - 6), min(40, w + 6))
    if got == w:
        return "подтверждена"
    if got and w % got == 0 or (got and got % w == 0):
        return "подтверждена"      # кратная ширина -- та же структура
    return "не сходится"


def unique_match(fw: Firmware, check, center: int, window: int):
    """Единственное совпадение в окне -> его адрес, иначе None."""
    hits = [center + dl for dl in range(-window, window + 1)
            if check(fw, center + dl)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        return None
    # несколько совпадений: берём ближайшее, только если оно заметно ближе
    hits.sort(key=lambda a: (abs(a - center), a))
    if abs(hits[0] - center) * 2 < abs(hits[1] - center):
        return hits[0]
    return None


# --------------------------------------------------------------------------
# Выравнивание
# --------------------------------------------------------------------------

class Alignment:
    """Кусочная функция сдвига, построенная по якорям."""

    def __init__(self, anchors: list[tuple[int, int]]) -> None:
        # anchors: список (исходный адрес, целевой адрес), отсортирован
        self.src = [a for a, _ in anchors]
        self.dst = [b for _, b in anchors]

    def shift_at(self, addr: int) -> int | None:
        if not self.src:
            return None
        i = bisect.bisect_left(self.src, addr)
        cands = []
        if i < len(self.src):
            cands.append(i)
        if i > 0:
            cands.append(i - 1)
        best = min(cands, key=lambda j: abs(self.src[j] - addr))
        return self.dst[best] - self.src[best]

    def map_addr(self, addr: int) -> int | None:
        sh = self.shift_at(addr)
        return None if sh is None else addr + sh

    def distance(self, addr: int) -> int:
        if not self.src:
            return 1 << 30
        i = bisect.bisect_left(self.src, addr)
        best = 1 << 30
        for j in (i - 1, i):
            if 0 <= j < len(self.src):
                best = min(best, abs(self.src[j] - addr))
        return best


# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Перенос разметки DAMOS на другую прошивку")
    ap.add_argument("--damos", required=True)
    ap.add_argument("--source", required=True, help="образ, к которому относится DAMOS")
    ap.add_argument("--target", required=True, help="образ, на который переносим")
    ap.add_argument("--window", type=int, default=512, help="окно поиска якоря")
    ap.add_argument("--max-anchor-dist", type=int, default=4096,
                    help="дальше этого расстояния от якоря выравнивание не применяем")
    ap.add_argument("--out")
    args = ap.parse_args(argv)

    d = damos.parse(args.damos)
    src = fwlib.load(args.source)
    tgt = fwlib.load(args.target)

    print("DAMOS: %d переменных" % len(d.variables), file=sys.stderr)

    # --- шаг 1: якоря --------------------------------------------------
    anchors: list[tuple[int, int]] = []
    anchor_of: dict[int, int] = {}     # idx -> целевой адрес
    verified_src = 0
    checkable = 0

    for v in d.variables:
        chk = signature(d, v)
        if chk is None:
            continue
        checkable += 1
        a = v.file_offset
        if not chk(src, a):
            continue                    # DAMOS не сходится с исходным образом
        verified_src += 1
        hit = unique_match(tgt, chk, a, args.window)
        if hit is not None:
            anchors.append((a, hit))
            anchor_of[v.idx] = hit

    anchors.sort()
    print("Записей с проверяемой структурой: %d" % checkable, file=sys.stderr)
    print("Подтверждено в исходном образе  : %d" % verified_src, file=sys.stderr)
    print("Найдено якорей в целевом образе : %d" % len(anchors), file=sys.stderr)

    from collections import Counter
    shifts = Counter(b - a for a, b in anchors)
    print("\nЧастые сдвиги: %s" % ", ".join(
        "%+d (%d)" % (s, c) for s, c in shifts.most_common(6)), file=sys.stderr)

    align = Alignment(anchors)

    # --- шаг 2: перенос всего -------------------------------------------
    out = []
    stat = Counter()
    for v in d.variables:
        w, h = d.dims(v)
        c = d.conv(v.conv_w)
        cx, cy = d.conv(v.conv_x), d.conv(v.conv_y)
        dw = conv_width(d, v.conv_w)

        if v.idx in anchor_of:
            addr, how = anchor_of[v.idx], "структурно"
        else:
            m = align.map_addr(v.file_offset)
            dist = align.distance(v.file_offset)
            if m is None or dist > args.max_anchor_dist:
                addr, how = None, "не перенесена"
            else:
                addr, how = m, "выравнивание"
        stat[how] += 1

        verdict = ""
        if addr is not None:
            verdict = verify_grid(tgt, data_offset(d, v, addr), w, h, dw)

        e = {
            "name": v.name, "desc": v.desc, "type": v.typ,
            "src_addr": v.file_offset, "addr": addr, "how": how,
            "verify": verdict, "ambiguous": False, "data_addr": data_offset(d, v, addr) if addr is not None else None,
            "width": w, "height": h, "data_width": dw,
            "signed": bool(c and c.phys_min < 0),
            "unit": c.unit if c else "",
            "factor": c.factor if c else 1.0,
            "shift": c.shift if c else 0.0,
            "conv": c.name if c else "",
            "phys_min": v.w_min, "phys_max": v.w_max,
            "x_conv": cx.name if cx else "", "x_unit": cx.unit if cx else "",
            "x_factor": cx.factor if cx else 0.0,
            "y_conv": cy.name if cy else "", "y_unit": cy.unit if cy else "",
            "y_factor": cy.factor if cy else 0.0,
            "x_ref": v.x_ref, "y_ref": v.y_ref,
        }
        out.append(e)

    # --- шаг 3: снять неоднозначности ------------------------------------
    # Выравнивание может посадить две разные переменные на один адрес либо
    # переставить соседние записи местами. Такие адреса помечаем: доверять
    # им нельзя, даже если каждая запись по отдельности выглядит правдоподобно.
    claims: dict[int, list[dict]] = {}
    for e in out:
        if e["addr"] is not None:
            claims.setdefault(e["addr"], []).append(e)
    ambiguous = 0
    for addr, group in claims.items():
        if len(group) < 2:
            continue
        # если одна из записей найдена структурно, она и права
        strong = [g for g in group if g["how"] == "структурно"]
        for g in group:
            if strong and g in strong and len(strong) == 1:
                continue
            g["ambiguous"] = True
            g["verify"] = "неоднозначно"
            ambiguous += 1

    print("\nПеренос: %s" % ", ".join("%s %d" % (k, n) for k, n in stat.most_common()),
          file=sys.stderr)
    print("Адресов, на которые претендует несколько имён: %d (записей: %d)"
          % (sum(1 for g in claims.values() if len(g) > 1), ambiguous), file=sys.stderr)

    maps = [e for e in out if e["addr"] is not None and e["width"] > 1 and e["height"] > 1]
    curves = [e for e in out if e["addr"] is not None and e["width"] > 1 and e["height"] == 1]
    print("Из них многомерных карт: %d, кривых: %d" % (len(maps), len(curves)),
          file=sys.stderr)
    vs = Counter(e["verify"] for e in maps)
    print("Независимая проверка тел карт: %s"
          % ", ".join("%s %d" % (k or "-", n) for k, n in vs.most_common()),
          file=sys.stderr)

    if args.out:
        json.dump({"damos": args.damos, "source": args.source,
                   "target": args.target,
                   "anchors": [{"src": a, "dst": b} for a, b in anchors],
                   "variables": out},
                  open(args.out, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
        print("\nЗаписано: %s" % args.out, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
