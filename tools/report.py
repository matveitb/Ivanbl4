#!/usr/bin/env python3
"""
report -- сборка итоговой разметки: профиль + результаты сканеров -> A2L / CSV / отчёт.

Объединяет три источника, в порядке убывания доверия:
  1. profiles/<профиль>.json -- проверенные вручную карты (высший приоритет);
  2. mapscan.py  -- карты классического формата Bosch (с заголовком и осями);
  3. gridscan.py -- голые таблицы без заголовка.

Пересекающиеся по адресам записи схлопываются: побеждает источник с большим
доверием, при равенстве -- более крупная таблица.

Использование:
    python3 tools/report.py --profile profiles/FBH3ID60.json \
        --maps out/maps_stock.json --grids out/grids_stock.json \
        --a2l out/FBH3ID60.a2l --csv out/FBH3ID60.csv --md out/РАЗМЕТКА.md
"""

from __future__ import annotations

import argparse
import json
import os
import sys

TYPE_OF = {1: "UBYTE", 2: "UWORD"}
TYPE_OF_SIGNED = {1: "SBYTE", 2: "SWORD"}


def data_type(e: dict) -> str:
    """Тип ячейки для A2L. Карты зажигания знаковые -- без этого -1 читается как 255."""
    tbl = TYPE_OF_SIGNED if e.get("signed") else TYPE_OF
    return tbl[e["data_width"]]
TRUST = {
    "профиль": 6,          # проверено вручную
    "damos-точно": 5,      # структурно найдено + тело подтверждено
    "damos": 4,            # структурно найдено по заголовку
    "mapscan": 3,
    "damos-вероятно": 2,   # перенесено выравниванием, тело правдоподобно
    "gridscan": 1,
    "damos-сомнительно": 0,
}


def _int(v) -> int:
    if isinstance(v, int):
        return v
    return int(str(v), 0)


# --------------------------------------------------------------------------
# Загрузка источников в единую схему
# --------------------------------------------------------------------------

def from_profile(path: str) -> tuple[dict, list[dict]]:
    prof = json.load(open(path, encoding="utf-8"))
    out = []
    for m in prof.get("maps", []):
        addr = _int(m["addr"])
        data_addr = _int(m.get("data_addr", addr))
        nx, ny = int(m["nx"]), int(m.get("ny", 1))
        dw = int(m.get("data_width", 1))
        aw = int(m.get("axis_width", 1))
        layout = m.get("layout", "bosch_header")
        end = data_addr + nx * ny * dw
        out.append({
            "name": m.get("name"), "layout": layout, "addr": addr,
            "data_addr": data_addr, "kind": m.get("kind", "3d"),
            "nx": nx, "ny": ny, "axis_width": aw, "data_width": dw,
            "end": end, "source": "профиль", "signed": bool(m.get("signed")),
            "factor": m.get("factor", 1.0), "shift": m.get("shift", 0.0),
            "unit": m.get("unit", ""), "conv": m.get("conv", ""),
            "confidence": m.get("confidence", "подтверждена"),
            "note": m.get("note", ""), "score": 1.0, "refs": 0,
        })
    return prof, out


def from_mapscan(path: str) -> list[dict]:
    d = json.load(open(path, encoding="utf-8"))
    out = []
    for m in d.get("maps", []):
        out.append({
            "name": None, "layout": "bosch_header", "addr": m["addr"],
            "data_addr": m["data_addr"], "kind": m["kind"],
            "nx": m["nx"], "ny": m["ny"],
            "axis_width": m["axis_width"], "data_width": m["data_width"],
            "end": m["end"], "source": "mapscan",
            "confidence": "вероятная", "note": "",
            "score": m.get("score", 0.0), "refs": m.get("refs", 0),
            "dmin": m.get("dmin", 0), "dmax": m.get("dmax", 0),
        })
    return out


def from_xfer(path: str) -> list[dict]:
    """
    Записи из tools/xfer.py -- имена, описания и МАСШТАБЫ из заводского DAMOS.

    Уровень доверия зависит от того, как найден адрес (структурно или
    выравниванием) и подтвердилось ли тело карты независимой проверкой.
    """
    d = json.load(open(path, encoding="utf-8"))
    out = []
    for v in d.get("variables", []):
        if v.get("addr") is None or v.get("width", 0) < 2:
            continue
        how, ver = v.get("how"), v.get("verify")
        if v.get("ambiguous"):
            src, conf = "damos-сомнительно", "неоднозначная"
        elif how == "структурно":
            src = "damos-точно" if ver == "подтверждена" else "damos"
            conf = "подтверждена"
        elif ver == "подтверждена":
            src, conf = "damos-точно", "подтверждена"
        elif ver == "не сходится":
            src, conf = "damos-сомнительно", "сомнительная"
        else:
            src, conf = "damos-вероятно", "вероятная"

        w, h = v["width"], v["height"]
        kind = "3d" if h > 1 else "2d"
        data_addr = v.get("data_addr") or v["addr"]
        dw = v.get("data_width", 1)
        out.append({
            "name": v["name"], "layout": "bare_grid", "addr": data_addr,
            "data_addr": data_addr, "kind": kind, "nx": w, "ny": h,
            "axis_width": 1, "data_width": dw,
            "end": data_addr + w * h * dw, "source": src,
            "confidence": conf, "note": v.get("desc", ""),
            "score": 1.0 if conf == "подтверждена" else 0.5, "refs": 0,
            "signed": v.get("signed", False),
            "factor": v.get("factor", 1.0), "shift": v.get("shift", 0.0),
            "unit": v.get("unit", ""), "conv": v.get("conv", ""),
            "x_unit": v.get("x_unit", ""), "y_unit": v.get("y_unit", ""),
            "damos_addr": v.get("src_addr"), "how": how, "verify": ver,
        })
    return out


def from_gridscan(path: str) -> list[dict]:
    d = json.load(open(path, encoding="utf-8"))
    out = []
    for g in d.get("grids", []):
        if g["dmin"] == g["dmax"]:
            note = "константная таблица (все значения %d)" % g["dmin"]
        else:
            note = ""
        out.append({
            "name": None, "layout": "bare_grid", "addr": g["addr"],
            "data_addr": g["addr"], "kind": "3d",
            "nx": g["width"], "ny": g["rows"],
            "axis_width": 1, "data_width": 1,
            "end": g["end"], "source": "gridscan",
            "confidence": "вероятная", "note": note,
            "score": max(0.0, 1.0 - g["score"] / 20.0), "refs": 0,
            "dmin": g.get("dmin", 0), "dmax": g.get("dmax", 0),
        })
    return out


# --------------------------------------------------------------------------

def merge(entries: list[dict]) -> list[dict]:
    """Схлопнуть пересекающиеся записи, оставив самый доверенный источник."""
    ranked = sorted(entries, key=lambda e: (
        -TRUST.get(e["source"], 0),
        -(e["end"] - e["addr"]),
        -e.get("score", 0.0),
        e["addr"],
    ))
    taken: list[dict] = []
    for e in ranked:
        lo, hi = min(e["addr"], e["data_addr"]), e["end"]
        if any(lo < t["end"] and min(t["addr"], t["data_addr"]) < hi for t in taken):
            continue
        taken.append(e)
    return sorted(taken, key=lambda e: e["addr"])


def autoname(e: dict, idx: int) -> str:
    if e.get("name"):
        return e["name"]
    dims = "%dx%d" % (e["nx"], e["ny"]) if e["kind"] == "3d" else "%d" % e["nx"]
    tag = "GRID" if e["layout"] == "bare_grid" else "MAP"
    return "%s_%03d_%05X_%s" % (tag, idx, e["addr"], dims)


def sanitize(name: str) -> str:
    return "".join(c if (c.isalnum() or c == "_") else "_" for c in name)


# --------------------------------------------------------------------------
# A2L
# --------------------------------------------------------------------------

A2L_HEAD = '''ASAP2_VERSION 1 61

/begin PROJECT %(proj)s "%(desc)s"

  /begin HEADER "%(desc)s"
    VERSION "1.0"
  /end HEADER

  /begin MODULE %(mod)s "Bosch M7.9.7 / Infineon C167"

    /begin MOD_COMMON "little-endian, выравнивание 1"
      BYTE_ORDER MSB_LAST
      ALIGNMENT_BYTE 1
      ALIGNMENT_WORD 1
      ALIGNMENT_LONG 1
    /end MOD_COMMON

    /begin COMPU_METHOD CM_IDENTITY
      "без пересчёта (сырые значения)"
      RAT_FUNC "%%.3f" ""
      COEFFS 0 1 0 0 0 1
    /end COMPU_METHOD
'''

A2L_TAIL = '''
  /end MODULE

/end PROJECT
'''


def compu_name(e: dict) -> str:
    """Имя COMPU_METHOD для этой записи (общее для одинаковых масштабов)."""
    f = e.get("factor")
    if not f or (abs(f - 1.0) < 1e-12 and not e.get("shift")):
        return "CM_IDENTITY"
    key = ("%.10g_%.10g" % (f, e.get("shift", 0.0))).replace("-", "n")
    key = key.replace(".", "p").replace("+", "").replace("e", "E")
    # единицы входят в ключ: множитель 0.75 встречается и у угла (grad KW),
    # и у относительной нагрузки (%) -- это разные пересчёты
    unit = "".join(c for c in (e.get("unit") or "") if c.isalnum())
    if unit:
        key += "_" + unit
    return "CM_" + key


def compu_body(e: dict) -> str:
    """
    ASAP2 RAT_FUNC переводит ФИЗИЧЕСКОЕ в СЫРОЕ:
        raw = (a*p^2 + b*p + c) / (d*p^2 + e*p + f)
    У нас phys = raw*factor - shift, значит raw = (phys + shift)/factor,
    то есть COEFFS 0 1 shift 0 0 factor.
    """
    name = compu_name(e)
    if name == "CM_IDENTITY":
        return ""
    f = e["factor"]
    sh = e.get("shift", 0.0)
    unit = (e.get("unit") or "").replace('"', "'")
    dec = 3 if f < 1 else 1
    return ('\n    /begin COMPU_METHOD %s\n      "%s"\n      RAT_FUNC "%%.%df" "%s"\n'
            '      COEFFS 0 1 %.10g 0 0 %.10g\n    /end COMPU_METHOD\n'
            % (name, e.get("conv") or ("масштаб %.6g" % f), dec, unit, sh, f))


def layout_name(e: dict) -> str:
    aw, dw = e["axis_width"], e["data_width"]
    sg = "S" if e.get("signed") else "U"
    if e["layout"] == "bare_grid":
        return "RL_GRID_D%s%d" % (sg, dw * 8)
    if e["kind"] == "3d":
        return "RL_3D_A%d_D%s%d" % (aw * 8, sg, dw * 8)
    return "RL_2D_A%d_D%s%d" % (aw * 8, sg, dw * 8)


def layout_body(e: dict) -> str:
    name = layout_name(e)
    aw, dw = e["axis_width"], e["data_width"]
    if e["layout"] == "bare_grid":
        return ('\n    /begin RECORD_LAYOUT %s\n'
                '      FNC_VALUES 1 %s ROW_DIR DIRECT\n'
                '    /end RECORD_LAYOUT\n' % (name, data_type(e)))
    if e["kind"] == "3d":
        return ('\n    /begin RECORD_LAYOUT %s\n'
                '      NO_AXIS_PTS_X 1 UBYTE\n'
                '      NO_AXIS_PTS_Y 2 UBYTE\n'
                '      AXIS_PTS_X    3 %s INDEX_INCR DIRECT\n'
                '      AXIS_PTS_Y    4 %s INDEX_INCR DIRECT\n'
                '      FNC_VALUES    5 %s ROW_DIR DIRECT\n'
                '    /end RECORD_LAYOUT\n'
                % (name, TYPE_OF[aw], TYPE_OF[aw], data_type(e)))
    return ('\n    /begin RECORD_LAYOUT %s\n'
            '      NO_AXIS_PTS_X 1 UBYTE\n'
            '      AXIS_PTS_X    2 %s INDEX_INCR DIRECT\n'
            '      FNC_VALUES    3 %s ROW_DIR DIRECT\n'
            '    /end RECORD_LAYOUT\n' % (name, TYPE_OF[aw], data_type(e)))


def axis_descr(n: int, aw: int, fixed: bool, cm: str = "CM_IDENTITY") -> str:
    amax = 0xFF if aw == 1 else 0xFFFF
    if fixed:
        # Голая таблица: реальных осей в блоке нет, подставляем индексную ось.
        # В WinOLS её потом можно заменить на настоящую.
        return ('\n      /begin AXIS_DESCR FIX_AXIS\n'
                '        NO_INPUT_QUANTITY\n        ' + cm + '\n'
                '        %d\n        0\n        %d\n'
                '        FIX_AXIS_PAR_DIST 0 1 %d\n'
                '      /end AXIS_DESCR' % (n, n - 1, n))
    return ('\n      /begin AXIS_DESCR STD_AXIS\n'
            '        NO_INPUT_QUANTITY\n        ' + cm + '\n'
            '        %d\n        0\n        %d\n'
            '      /end AXIS_DESCR' % (n, amax))


def characteristic(e: dict, name: str) -> str:
    is3d = e["kind"] == "3d"
    ctype = "MAP" if is3d else "CURVE"
    fixed = e["layout"] == "bare_grid"
    if e.get("signed"):
        dmax = 0x7F if e["data_width"] == 1 else 0x7FFF
    else:
        dmax = 0xFF if e["data_width"] == 1 else 0xFFFF
    body = axis_descr(e["nx"], e["axis_width"], fixed)
    if is3d:
        body += axis_descr(e["ny"], e["axis_width"], fixed)
    cm = compu_name(e)
    cmt = "%s | %s | адрес 0x%X, данные 0x%X" % (
        e.get("confidence", ""), e.get("source", ""), e["addr"], e["data_addr"])
    if e.get("factor") and e["factor"] != 1.0:
        cmt += " | масштаб x%.6g %s" % (e["factor"], e.get("unit", ""))
    if e.get("note"):
        cmt += " | " + e["note"]
    cmt = cmt.replace('"', "'")
    return ('\n    /begin CHARACTERISTIC %s\n      "%s"\n      %s\n      0x%X\n'
            '      %s\n      0\n      %s\n      0\n      %d%s\n'
            '    /end CHARACTERISTIC\n'
            % (name, cmt, ctype, e["addr"], layout_name(e), cm, dmax, body))


# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Сборка итоговой разметки")
    ap.add_argument("--profile")
    ap.add_argument("--maps", help="JSON от mapscan.py")
    ap.add_argument("--grids", help="JSON от gridscan.py")
    ap.add_argument("--xfer", help="JSON от xfer.py (имена и масштабы из DAMOS)")
    ap.add_argument("--min-score", type=float, default=0.0)
    ap.add_argument("--a2l")
    ap.add_argument("--csv")
    ap.add_argument("--md")
    ap.add_argument("--json", dest="json_out")
    ap.add_argument("--project", default="FBH3ID60")
    ap.add_argument("--desc", default="Kia Spectra 1.6 Bosch M7.9.7 FBH3ID60")
    args = ap.parse_args(argv)

    prof: dict = {}
    entries: list[dict] = []
    if args.profile:
        prof, pe = from_profile(args.profile)
        entries += pe
    if args.xfer and os.path.exists(args.xfer):
        entries += from_xfer(args.xfer)
    if args.maps and os.path.exists(args.maps):
        entries += from_mapscan(args.maps)
    if args.grids and os.path.exists(args.grids):
        entries += from_gridscan(args.grids)

    if not entries:
        print("Нет входных данных", file=sys.stderr)
        return 2

    entries = [e for e in entries
               if e["source"] == "профиль" or e.get("score", 0) >= args.min_score]
    merged = merge(entries)
    for i, e in enumerate(merged):
        e["name"] = sanitize(autoname(e, i))

    by_src: dict[str, int] = {}
    for e in merged:
        by_src[e["source"]] = by_src.get(e["source"], 0) + 1
    print("Итого карт: %d  (%s)" %
          (len(merged), ", ".join("%s: %d" % kv for kv in sorted(by_src.items()))))

    if args.json_out:
        json.dump({"profile": prof.get("profile"), "maps": merged},
                  open(args.json_out, "w", encoding="utf-8"),
                  indent=1, ensure_ascii=False)
        print("JSON:", args.json_out)

    if args.a2l:
        layouts: dict[str, str] = {}
        compus: dict[str, str] = {}
        chars = []
        for e in merged:
            layouts.setdefault(layout_name(e), layout_body(e))
            cb = compu_body(e)
            if cb:
                compus.setdefault(compu_name(e), cb)
            chars.append(characteristic(e, e["name"]))
        with open(args.a2l, "w", encoding="utf-8") as fh:
            fh.write(A2L_HEAD % {"proj": args.project, "desc": args.desc,
                                 "mod": args.project + "_MOD"})
            for b in compus.values():
                fh.write(b)
            for b in layouts.values():
                fh.write(b)
            for c in chars:
                fh.write(c)
            fh.write(A2L_TAIL)
        print("A2L :", args.a2l, "(%d карт, %d раскладок, %d пересчётов)"
              % (len(merged), len(layouts), len(compus) + 1))

    if args.csv:
        with open(args.csv, "w", encoding="utf-8-sig", newline="") as fh:
            fh.write("Имя;Адрес;Адрес данных;Формат;Тип;Столбцов;Строк;Разр.данных;"
                     "Множитель;Смещение;Единицы;Достоверность;Источник;Описание\n")
            for e in merged:
                fh.write("%s;0x%X;0x%X;%s;%s;%d;%d;%d;%s;%s;%s;%s;%s;%s\n" % (
                    e["name"], e["addr"], e["data_addr"],
                    "с заголовком" if e["layout"] == "bosch_header" else "голая сетка",
                    e["kind"], e["nx"], e["ny"], e["data_width"] * 8,
                    ("%.8g" % e["factor"]) if e.get("factor") else "",
                    ("%.8g" % e["shift"]) if e.get("shift") else "",
                    e.get("unit", ""),
                    e.get("confidence", ""), e.get("source", ""),
                    (e.get("note") or "").replace(";", ",")))
        print("CSV :", args.csv)

    if args.md:
        with open(args.md, "w", encoding="utf-8") as fh:
            fh.write("# Разметка прошивки %s\n\n" % prof.get("profile", ""))
            ident = prof.get("identification", {})
            if ident:
                fh.write("## Идентификация блока\n\n")
                fh.write("| Поле | Значение |\n|---|---|\n")
                for k, label in (("bosch_hw", "Номер блока Bosch"),
                                 ("bosch_sw", "Номер ПО Bosch"),
                                 ("calibration", "Калибровка"),
                                 ("oem_part", "Номер OEM")):
                    if ident.get(k):
                        fh.write("| %s | `%s` |\n" % (label, ident[k]))
                fh.write("\nСтрока в прошивке по адресу %s:\n\n```\n%s\n```\n\n"
                         % (ident.get("addr", "?"), ident.get("string", "")))
            cs = prof.get("checksum", {})
            if cs:
                fh.write("## Контрольные суммы\n\n")
                fh.write("- Таблица: **%s**, записи по %d байт\n"
                         % (cs.get("table_addr"), cs.get("record_size", 16)))
                fh.write("- Формат записи: `%s`\n" % cs.get("record_layout"))
                fh.write("- Алгоритм: %s\n" % cs.get("algorithm"))
                fh.write("- Проверка: %s\n\n" % cs.get("verified"))
                fh.write("Пересчёт: `python3 tools/bosch_csum.py fix вход.bin -o выход.bin`\n\n")

            conf = [e for e in merged if e.get("confidence") == "подтверждена"]
            prob = [e for e in merged if e.get("confidence") != "подтверждена"]

            fh.write("## Подтверждённые карты (%d)\n\n" % len(conf))
            fh.write("| Имя | Адрес | Данные | Размер | Формат | Примечание |\n")
            fh.write("|---|---|---|---|---|---|\n")
            for e in conf:
                dims = ("%dx%d" % (e["nx"], e["ny"])) if e["kind"] == "3d" else str(e["nx"])
                fh.write("| `%s` | `0x%X` | `0x%X` | %s u%d | %s | %s |\n" % (
                    e["name"], e["addr"], e["data_addr"], dims, e["data_width"] * 8,
                    "заголовок" if e["layout"] == "bosch_header" else "голая сетка",
                    e.get("note", "")))

            fh.write("\n## Вероятные карты (%d)\n\n" % len(prob))
            fh.write("| Имя | Адрес | Данные | Размер | Формат | Источник | Значения |\n")
            fh.write("|---|---|---|---|---|---|---|\n")
            for e in prob:
                dims = ("%dx%d" % (e["nx"], e["ny"])) if e["kind"] == "3d" else str(e["nx"])
                fh.write("| `%s` | `0x%X` | `0x%X` | %s u%d | %s | %s | %s..%s |\n" % (
                    e["name"], e["addr"], e["data_addr"], dims, e["data_width"] * 8,
                    "заголовок" if e["layout"] == "bosch_header" else "сетка",
                    e.get("source", ""), e.get("dmin", ""), e.get("dmax", "")))
            fh.write("\n> Вероятные карты найдены эвристикой и требуют проверки "
                     "глазами в WinOLS: у настоящей карты поверхность гладкая, "
                     "а оси монотонны.\n")
        print("Отчёт:", args.md)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
