#!/usr/bin/env python3
"""
gen_sfr -- построить таблицу регистров C167 из даташита Infineon.

Раньше карта регистров была записана по памяти и проверялась косвенно.
Владелец прислал официальный даташит C167CR/C167SR (V3.3, 2005-02), и
теперь она берётся оттуда: раздел 3.15 "Special Function Registers
Overview", таблица 8.

Сам PDF в репозиторий не кладётся -- он чужой. Кладётся извлечённая
фактическая таблица (имена, адреса, короткие адреса) в data/c167_sfr.json
и этот скрипт, чтобы её можно было получить заново:

    pdftotext -layout C167CR_datasheet.pdf c167.txt
    python3 tools/gen_sfr.py c167.txt --out data/c167_sfr.json

Формат строки таблицы:

    ADCON      b FFA0H        D0H     A/D Converter Control Register    0000H
    ADDAT2       F0A0H    E 50H       A/D Converter 2 Result Register   0000H

где "b" -- битадресуемый, "E" -- расширенное пространство ESFR.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

ROW = re.compile(
    r'^\s*([A-Z][A-Z0-9_]{1,11})\s+(b\s+)?([0-9A-F]{4})H\s+([EX]\s+)?'
    r'([0-9A-F]{2})H\s+(.+?)\s*$')
TAIL = re.compile(r'\s+[0-9A-FX]{4}H?$')


def parse(lines: list[str]) -> dict:
    out = {}
    for i, ln in enumerate(lines):
        m = ROW.match(ln)
        if not m:
            continue
        desc = TAIL.sub("", m.group(6)).strip()
        # описание бывает перенесено на следующую строку -- она с большим
        # отступом и сама под шаблон строки таблицы не подходит
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if nxt.strip() and not ROW.match(nxt) \
                and len(nxt) - len(nxt.lstrip()) > 30:
            desc = TAIL.sub("", desc + " " + nxt.strip()).strip()
        out[m.group(1)] = dict(
            addr=int(m.group(3), 16),
            sreg=int(m.group(5), 16),
            space=(m.group(4) or " ").strip() or "S",
            bit=bool(m.group(2)),
            desc=desc)
    return out


def check(tbl: dict) -> list[str]:
    """
    Короткий адрес обязан быть (адрес - база) / 2, база 0xFE00 для SFR и
    0xF000 для ESFR. Это не догадка: формула проверяется на всей таблице
    разом, и расхождения выписываются, а не заминаются.
    """
    bad = []
    for n, v in tbl.items():
        base = 0xF000 if v["space"] == "E" else 0xFE00
        lo, hi = (0xF000, 0xF1FF) if v["space"] == "E" else (0xFE00, 0xFFFF)
        if not lo <= v["addr"] <= hi:
            continue
        want = (v["addr"] - base) // 2
        if want != v["sreg"]:
            bad.append("%s: 0x%04X даёт 0x%02X, в таблице 0x%02X"
                       % (n, v["addr"], want, v["sreg"]))
    return bad


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Таблица регистров C167 из даташита")
    ap.add_argument("text", help="результат pdftotext -layout")
    ap.add_argument("--out", default="data/c167_sfr.json")
    a = ap.parse_args(argv)

    lines = open(a.text, encoding="utf-8", errors="replace").read().split("\n")
    tbl = parse(lines)
    if not tbl:
        print("таблица не нашлась -- проверьте, что это даташит C167",
              file=sys.stderr)
        return 1

    bad = check(tbl)
    sfr = sum(1 for v in tbl.values() if v["space"] == "S")
    esfr = sum(1 for v in tbl.values() if v["space"] == "E")
    print("регистров: %d (SFR %d, ESFR %d)" % (len(tbl), sfr, esfr))
    print("сошлось с формулой короткого адреса: %d из %d"
          % (len(tbl) - len(bad), len(tbl)))
    for b in bad:
        print("  РАСХОЖДЕНИЕ В ДАТАШИТЕ: " + b)

    json.dump(dict(source="Infineon C167CR/C167SR Data Sheet V3.3, 2005-02, "
                          "таблица 8, раздел 3.15",
                   discrepancies=bad,
                   registers=tbl),
              open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("записано " + a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
