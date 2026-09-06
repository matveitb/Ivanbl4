#!/usr/bin/env python3
"""
a2lgen -- экспорт найденных карт в A2L (ASAP2) и CSV для импорта в WinOLS.

WinOLS умеет импортировать A2L. Раскладка записи (RECORD_LAYOUT) описывается
ровно так, как карты лежат в прошивке Bosch:

    NO_AXIS_PTS_X -> nx
    NO_AXIS_PTS_Y -> ny
    AXIS_PTS_X    -> ось X
    AXIS_PTS_Y    -> ось Y
    FNC_VALUES    -> тело карты

поэтому адрес CHARACTERISTIC = адрес заголовка карты, и WinOLS сам разберёт
оси и размерности.

Использование:
    python3 tools/mapscan.py firmware/stock.bin --out out/maps.json
    python3 tools/a2lgen.py out/maps.json --a2l out/stock.a2l --csv out/stock.csv
    python3 tools/a2lgen.py out/maps.json --names out/names.json --a2l out/stock.a2l
"""

from __future__ import annotations

import argparse
import json
import os
import sys


TYPE_OF = {1: "UBYTE", 2: "UWORD"}


HEADER = '''ASAP2_VERSION 1 61

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

FOOTER = '''
  /end MODULE

/end PROJECT
'''


def record_layout_3d(aw: int, dw: int) -> tuple[str, str]:
    name = "RL_3D_A%d_D%d" % (aw * 8, dw * 8)
    body = '''
    /begin RECORD_LAYOUT %s
      NO_AXIS_PTS_X 1 UBYTE
      NO_AXIS_PTS_Y 2 UBYTE
      AXIS_PTS_X    3 %s INDEX_INCR DIRECT
      AXIS_PTS_Y    4 %s INDEX_INCR DIRECT
      FNC_VALUES    5 %s ROW_DIR DIRECT
    /end RECORD_LAYOUT
''' % (name, TYPE_OF[aw], TYPE_OF[aw], TYPE_OF[dw])
    return name, body


def record_layout_2d(aw: int, dw: int) -> tuple[str, str]:
    name = "RL_2D_A%d_D%d" % (aw * 8, dw * 8)
    body = '''
    /begin RECORD_LAYOUT %s
      NO_AXIS_PTS_X 1 UBYTE
      AXIS_PTS_X    2 %s INDEX_INCR DIRECT
      FNC_VALUES    3 %s ROW_DIR DIRECT
    /end RECORD_LAYOUT
''' % (name, TYPE_OF[aw], TYPE_OF[dw])
    return name, body


def characteristic(m: dict, name: str, rl: str, comment: str) -> str:
    is3d = m["kind"] == "3d"
    ctype = "MAP" if is3d else "CURVE"
    dmax_type = 0xFF if m["data_width"] == 1 else 0xFFFF
    amax_type = 0xFF if m["axis_width"] == 1 else 0xFFFF

    x_descr = '''
      /begin AXIS_DESCR STD_AXIS
        NO_INPUT_QUANTITY
        CM_IDENTITY
        %d
        0
        %d
      /end AXIS_DESCR''' % (m["nx"], amax_type)

    y_descr = ""
    if is3d:
        y_descr = '''
      /begin AXIS_DESCR STD_AXIS
        NO_INPUT_QUANTITY
        CM_IDENTITY
        %d
        0
        %d
      /end AXIS_DESCR''' % (m["ny"], amax_type)

    return '''
    /begin CHARACTERISTIC %s
      "%s"
      %s
      0x%X
      %s
      0
      CM_IDENTITY
      0
      %d%s%s
    /end CHARACTERISTIC
''' % (name, comment, ctype, m["addr"], rl, dmax_type, x_descr, y_descr)


def default_name(m: dict, idx: int) -> str:
    dims = "%dx%d" % (m["nx"], m["ny"]) if m["kind"] == "3d" else "%d" % m["nx"]
    return "MAP_%03d_%06X_%s" % (idx, m["addr"], dims)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Экспорт карт в A2L / CSV для WinOLS")
    ap.add_argument("maps_json", help="результат mapscan.py --out")
    ap.add_argument("--a2l", help="путь для записи A2L")
    ap.add_argument("--csv", help="путь для записи CSV")
    ap.add_argument("--names", help="JSON {\"0x18000\": \"IGN_MAIN\", ...} с именами карт")
    ap.add_argument("--min-score", type=float, default=0.0)
    ap.add_argument("--project", default="M797_PROJECT")
    ap.add_argument("--desc", default="Kia Spectra 1.6 Bosch M7.9.7")
    args = ap.parse_args(argv)

    payload = json.load(open(args.maps_json, encoding="utf-8"))
    maps = [m for m in payload["maps"] if m.get("score", 1.0) >= args.min_score]
    maps.sort(key=lambda m: m["addr"])

    names: dict[str, str] = {}
    if args.names and os.path.exists(args.names):
        raw = json.load(open(args.names, encoding="utf-8"))
        for k, v in raw.items():
            names[str(int(k, 0))] = v

    if not args.a2l and not args.csv:
        print("Укажите --a2l и/или --csv", file=sys.stderr)
        return 2

    # ---- A2L ---------------------------------------------------------
    if args.a2l:
        layouts: dict[str, str] = {}
        chars: list[str] = []
        for i, m in enumerate(maps):
            aw, dw = m["axis_width"], m["data_width"]
            if m["kind"] == "3d":
                rl, body = record_layout_3d(aw, dw)
            else:
                rl, body = record_layout_2d(aw, dw)
            layouts.setdefault(rl, body)

            nm = names.get(str(m["addr"]), default_name(m, i))
            nm = "".join(c if (c.isalnum() or c == "_") else "_" for c in nm)
            comment = "адрес 0x%X, данные 0x%X, оценка %.2f, гладкость %.2f, xref %d" % (
                m["addr"], m["data_addr"], m.get("score", 0), m.get("smooth", 0),
                m.get("refs", 0))
            chars.append(characteristic(m, nm, rl, comment))

        with open(args.a2l, "w", encoding="utf-8") as fh:
            fh.write(HEADER % {"proj": args.project, "desc": args.desc,
                               "mod": args.project + "_MOD"})
            for body in layouts.values():
                fh.write(body)
            for c in chars:
                fh.write(c)
            fh.write(FOOTER)
        print("A2L записан: %s (%d карт, %d раскладок)" %
              (args.a2l, len(maps), len(layouts)))

    # ---- CSV ---------------------------------------------------------
    if args.csv:
        with open(args.csv, "w", encoding="utf-8-sig", newline="") as fh:
            fh.write("Имя;Адрес заголовка;Адрес данных;Тип;Столбцов (X);Строк (Y);"
                     "Разрядность оси;Разрядность данных;Мин;Макс;Оценка;XREF\n")
            for i, m in enumerate(maps):
                nm = names.get(str(m["addr"]), default_name(m, i))
                fh.write("%s;0x%X;0x%X;%s;%d;%d;%d;%d;%d;%d;%.2f;%d\n" % (
                    nm, m["addr"], m["data_addr"], m["kind"], m["nx"], m["ny"],
                    m["axis_width"] * 8, m["data_width"] * 8,
                    m.get("dmin", 0), m.get("dmax", 0),
                    m.get("score", 0), m.get("refs", 0)))
        print("CSV записан: %s (%d карт)" % (args.csv, len(maps)))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
