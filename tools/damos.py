#!/usr/bin/env python3
"""
damos -- разбор файла DAMOS (.dam, формат ASAP2DAM) от Bosch.

DAMOS -- это заводское описание калибровок: имена переменных, тексты описаний,
адреса, размерности и, главное, ФОРМУЛЫ ПЕРЕСЧЁТА в физические единицы.

Грамматика (установлена по px5ns03d.dam и проверена на его же образе):

    <idx>, /SPZ, <ИМЯ>, {<описание>}, <тип>, <адрес>, <адрес2>
    /SPW, <id_пересчёта>, <знаков>, <физ_мин>, <физ_макс>      -- значения
    /SPX, <id_пересчёта>, <знаков>, <мин>, <макс>, <ссылка>    -- ось X (строки)
    /SPY, <id_пересчёта>, <знаков>, <мин>, <макс>, <ссылка>    -- ось Y (столбцы)
    /FKX, <точек_X>, 0, 0
    /FKY, <точек_Y>, 0, 0
    /ABL, <n>;

Пересчёт описан парой записей:

    <idx>, /REG, <имя>, {}, <тип>, <?>, {<единица>}, <знаков>, <?>, <мин>, <макс>
    /REP, <raw_max>, <смещение>, <?>, <размах>, 0, 0;

и вычисляется как:

    физ = сырое * размах / raw_max - смещение / raw_max

Проверено: для zw_ub_q0p75 это даёт ровно 0.75 град/ед., что совпадает с
именем пересчёта (q0p75) и с диапазоном 0..191.25 из самого DAMOS.

ВАЖНО о порядке осей: в этом DAMOS ось Y меняется быстрее -- то есть
Y это СТОЛБЦЫ, X это СТРОКИ. Установлено по KFZWOP: ось X = SNM16OPUB
(обороты, 16 точек), ось Y = SRL11OPUW (нагрузка, 11 точек), а карта в
образе лежит как 11 в ширину и 16 в высоту.

Использование:
    python3 tools/damos.py firmware/px5ns03d.dam --list --grep ZW
    python3 tools/damos.py firmware/px5ns03d.dam --json out/damos.json
    python3 tools/damos.py firmware/px5ns03d.dam --name KFZWOP --verbose
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict, field


# --------------------------------------------------------------------------

@dataclass
class Conversion:
    idx: int
    name: str
    unit: str = ""
    decimals: int = 0
    phys_min: float = 0.0
    phys_max: float = 0.0
    raw_max: int = 0
    offset: float = 0.0
    span: float = 0.0

    @property
    def factor(self) -> float:
        if not self.raw_max:
            return 1.0
        return self.span / self.raw_max

    @property
    def shift(self) -> float:
        if not self.raw_max:
            return 0.0
        return self.offset / self.raw_max

    def to_phys(self, raw: float) -> float:
        return raw * self.factor - self.shift

    def describe(self) -> str:
        if not self.raw_max:
            return "%s (без пересчёта)" % self.name
        s = "phys = raw * %.6g" % self.factor
        if self.shift:
            s += " - %.6g" % self.shift
        if self.unit:
            s += "  [%s]" % self.unit
        return s


@dataclass
class Variable:
    idx: int
    name: str
    desc: str
    typ: int
    addr: int
    addr2: int
    conv_w: int = 0          # id пересчёта значений
    w_min: float = 0.0
    w_max: float = 0.0
    conv_x: int = 0
    x_min: float = 0.0
    x_max: float = 0.0
    x_ref: int = 0           # ссылка на внешнюю ось (для карт с общими осями)
    conv_y: int = 0
    y_min: float = 0.0
    y_max: float = 0.0
    y_ref: int = 0
    nx: int = 0              # /FKX -- точек по X
    ny: int = 0              # /FKY -- точек по Y
    abl: int = 0

    @property
    def file_offset(self) -> int:
        return self.addr - 0x800000 if self.addr >= 0x800000 else self.addr


# --------------------------------------------------------------------------

_SPZ = re.compile(r'^(\d+),\s*/SPZ,\s*([^,]+),\s*\{(.*?)\},\s*(\d+),\s*\$([0-9A-Fa-f]+),\s*\$([0-9A-Fa-f]+)')
_REG = re.compile(r'^(\d+),\s*/REG,\s*([^,]+),\s*\{(.*?)\},\s*(\d+),\s*([^,]+),\s*\{(.*?)\},\s*(\d+),\s*([^,]+),\s*([^,]+),\s*(.+)$')


def _nums(line: str) -> list[str]:
    body = line.split(",", 1)[1] if "," in line else ""
    return [p.strip().rstrip(";").strip() for p in body.split(",")]


def _f(s: str, default: float = 0.0) -> float:
    try:
        return float(s)
    except (ValueError, TypeError):
        return default


def _i(s: str, default: int = 0) -> int:
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return default


class Damos:
    def __init__(self) -> None:
        self.variables: list[Variable] = []
        self.conversions: dict[int, Conversion] = {}
        self.segments: list[tuple[str, int, int]] = []
        self.header: dict[str, str] = {}

    # -- поиск -----------------------------------------------------------

    def by_name(self, name: str) -> Variable | None:
        for v in self.variables:
            if v.name == name:
                return v
        return None

    def by_index(self, idx: int) -> Variable | None:
        for v in self.variables:
            if v.idx == idx:
                return v
        return None

    def conv(self, cid: int) -> Conversion | None:
        return self.conversions.get(cid)

    # -- размерность карты ------------------------------------------------

    def dims(self, v: Variable) -> tuple[int, int]:
        """
        Вернуть (ширина, высота) в том виде, как карта лежит в образе.

        Ось Y меняется быстрее -> Y это столбцы (ширина), X -- строки (высота).
        Если счётчики нулевые, они берутся из записей внешних осей.
        """
        nx, ny = v.nx, v.ny
        if not nx and v.x_ref:
            ax = self.by_index(v.x_ref)
            if ax:
                nx = ax.nx
        if not ny and v.y_ref:
            ay = self.by_index(v.y_ref)
            if ay:
                ny = ay.nx      # у записи оси число точек лежит в /FKX
        if not ny:
            # одномерная величина: единственная ось лежит в /FKX
            return (nx, 1) if nx else (0, 0)
        return ny, nx           # (ширина=Y, высота=X)


def parse(path: str) -> Damos:
    d = Damos()
    cur_var: Variable | None = None
    cur_conv: Conversion | None = None

    with open(path, encoding="latin-1") as fh:
        lines = fh.read().splitlines()

    for line in lines:
        line = line.strip()
        if not line:
            continue

        if line.startswith("/SND,"):
            # формат: "/SND, CODE1 $800000 $80FFFF" -- поля разделены пробелами
            body = line.split(",", 1)[1].strip().rstrip(";")
            parts = body.split()
            if len(parts) >= 3 and parts[1].startswith("$") and parts[2].startswith("$"):
                try:
                    d.segments.append((parts[0], int(parts[1][1:], 16),
                                       int(parts[2][1:], 16)))
                except ValueError:
                    pass
            continue

        if line.startswith("/EPK,") or line.startswith("/SGB,") or line.startswith("/EPR,"):
            d.header[line.split(",")[0].lstrip("/")] = line.split(",", 1)[1].strip()
            continue

        m = _SPZ.match(line)
        if m:
            cur_var = Variable(
                idx=int(m.group(1)), name=m.group(2).strip(), desc=m.group(3).strip(),
                typ=int(m.group(4)), addr=int(m.group(5), 16), addr2=int(m.group(6), 16))
            d.variables.append(cur_var)
            cur_conv = None
            continue

        m = _REG.match(line)
        if m:
            cur_conv = Conversion(
                idx=int(m.group(1)), name=m.group(2).strip(),
                unit=m.group(6).strip(), decimals=_i(m.group(7)),
                phys_min=_f(m.group(9)), phys_max=_f(m.group(10)))
            d.conversions[cur_conv.idx] = cur_conv
            cur_var = None
            continue

        if line.startswith("/REP,") and cur_conv is not None:
            p = _nums(line)
            cur_conv.raw_max = _i(p[0]) if len(p) > 0 else 0
            cur_conv.offset = _f(p[1]) if len(p) > 1 else 0.0
            cur_conv.span = _f(p[3]) if len(p) > 3 else 0.0
            continue

        if cur_var is None:
            continue

        p = _nums(line)
        if line.startswith("/SPW,"):
            cur_var.conv_w = _i(p[0]) if p else 0
            cur_var.w_min = _f(p[2]) if len(p) > 2 else 0.0
            cur_var.w_max = _f(p[3]) if len(p) > 3 else 0.0
        elif line.startswith("/SPX,"):
            cur_var.conv_x = _i(p[0]) if p else 0
            cur_var.x_min = _f(p[2]) if len(p) > 2 else 0.0
            cur_var.x_max = _f(p[3]) if len(p) > 3 else 0.0
            cur_var.x_ref = _i(p[4]) if len(p) > 4 else 0
        elif line.startswith("/SPY,"):
            cur_var.conv_y = _i(p[0]) if p else 0
            cur_var.y_min = _f(p[2]) if len(p) > 2 else 0.0
            cur_var.y_max = _f(p[3]) if len(p) > 3 else 0.0
            cur_var.y_ref = _i(p[4]) if len(p) > 4 else 0
        elif line.startswith("/FKX,"):
            cur_var.nx = _i(p[0]) if p else 0
        elif line.startswith("/FKY,"):
            cur_var.ny = _i(p[0]) if p else 0
        elif line.startswith("/ABL,"):
            cur_var.abl = _i(p[0]) if p else 0
            cur_var = None

    return d


# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Разбор DAMOS")
    ap.add_argument("damos")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--grep", help="фильтр по имени или описанию (регистронезависимо)")
    ap.add_argument("--name", help="показать одну переменную подробно")
    ap.add_argument("--type", type=int, help="фильтр по типу записи")
    ap.add_argument("--maps-only", action="store_true", help="только многомерные (nx>1)")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--json", dest="json_out")
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args(argv)

    d = parse(args.damos)
    print("Переменных: %d, пересчётов: %d, сегментов: %d"
          % (len(d.variables), len(d.conversions), len(d.segments)), file=sys.stderr)

    if args.stats:
        print("\nСегменты:")
        for name, a, b in d.segments:
            print("  %-10s $%06X..$%06X  (файл 0x%05X..0x%05X)"
                  % (name, a, b, a - 0x800000, b - 0x800000))
        from collections import Counter
        print("\nТипы записей:")
        for t, n in sorted(Counter(v.typ for v in d.variables).items()):
            sample = next(v for v in d.variables if v.typ == t)
            w, h = d.dims(sample)
            shape = "%d x %d" % (w, h) if max(w, h) > 1 else "скаляр"
            print("  тип %-3d : %5d шт.  пример %-12s %s"
                  % (t, n, sample.name, shape))
        return 0

    if args.name:
        v = d.by_name(args.name)
        if not v:
            print("Не найдено: %s" % args.name, file=sys.stderr)
            return 1
        w, h = d.dims(v)
        print("Имя        : %s" % v.name)
        print("Описание   : %s" % v.desc)
        print("Тип        : %d" % v.typ)
        print("Адрес      : $%06X  (файл 0x%05X)" % (v.addr, v.file_offset))
        print("Размер     : %d x %d  (ширина=Y, высота=X)" % (w, h))
        cw = d.conv(v.conv_w)
        if cw:
            print("Значения   : %s" % cw.describe())
            print("             диапазон %.4g..%.4g %s" % (v.w_min, v.w_max, cw.unit))
        for label, cid, ref in (("Ось X (строки)", v.conv_x, v.x_ref),
                                ("Ось Y (столбцы)", v.conv_y, v.y_ref)):
            c = d.conv(cid)
            if c:
                line = "%-16s: %s" % (label, c.describe())
                if ref:
                    ax = d.by_index(ref)
                    if ax:
                        line += "  -> %s @0x%05X, %d точек" % (ax.name, ax.file_offset, ax.nx)
                print(line)
        return 0

    sel = d.variables
    if args.grep:
        q = args.grep.lower()
        sel = [v for v in sel if q in v.name.lower() or q in v.desc.lower()]
    if args.type is not None:
        sel = [v for v in sel if v.typ == args.type]
    if args.maps_only:
        sel = [v for v in sel if max(d.dims(v)) > 1]

    if args.list or args.grep or args.type is not None or args.maps_only:
        print("%-14s %-8s %-5s %-9s  %s" % ("ИМЯ", "АДРЕС", "ТИП", "РАЗМЕР", "ОПИСАНИЕ"))
        for v in sel:
            w, h = d.dims(v)
            dims = "%d x %d" % (w, h) if max(w, h) > 1 else "скаляр"
            print("%-14s 0x%05X  %-5d %-9s  %s"
                  % (v.name, v.file_offset, v.typ, dims, v.desc[:70]))
        print("\nПоказано: %d" % len(sel), file=sys.stderr)

    if args.json_out:
        payload = {
            "source": args.damos,
            "header": d.header,
            "segments": [{"name": n, "start": a, "end": b} for n, a, b in d.segments],
            "conversions": {str(k): asdict(c) for k, c in d.conversions.items()},
            "variables": [],
        }
        for v in d.variables:
            w, h = d.dims(v)
            e = asdict(v)
            e["file_offset"] = v.file_offset
            e["width"] = w
            e["height"] = h
            cw = d.conv(v.conv_w)
            if cw:
                e["factor"] = cw.factor
                e["shift"] = cw.shift
                e["unit"] = cw.unit
                e["conv_name"] = cw.name
            payload["variables"].append(e)
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1, ensure_ascii=False)
        print("Записано: %s" % args.json_out, file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
