#!/usr/bin/env python3
"""
flags -- таблицы флагов диагностики: классы неисправностей (CLA*) и
кодовые слова (CD*), с именами и описаниями из DAMOS.

В Bosch M7 обе таблицы -- сплошные массивы по одному байту на переменную,
идущие в одном и том же порядке во всех прошивках семейства. Поэтому
достаточно найти сдвиг таблицы в целевой прошивке, и все 67 классов
неисправностей и все кодовые слова получают имена.

Сдвиг определяется по опорным переменным: берутся адреса из DAMOS, к ним
прибавляется кандидат сдвига, и проверяется, что байты во всех прошивках
имеют вид допустимых значений класса (0, 3, 5 и т.п.). Дополнительно
сдвиг подтверждается тем, что группы изменившихся байтов в прошивках
Euro 0/2/3 ложатся ровно на переменные второго датчика кислорода.

Установлено для FBH3ID60: таблица CLA* начинается с 0x11D92
(в px5ns03d она с 0x118C3, сдвиг +0x4CF).

Использование:
    python3 tools/flags.py --prefix CLA firmware/*.bin
    python3 tools/flags.py --prefix CD --changed-only firmware/*.bin
"""

from __future__ import annotations

import argparse
import os
import sys

import fwlib
import damos

# Опорная точка: DAMOS-адрес первой переменной таблицы -> адрес в FBH3ID60
TABLE_BASE = {
    "CLA": (0x118C3, 0x11D92),   # CLAAAA "Dummy: top of table"
    "CD":  (0x10002, 0x10003),   # CDAGR
}


def table_vars(d: damos.Damos, prefix: str):
    src_base, dst_base = TABLE_BASE[prefix]
    shift = dst_base - src_base
    out = []
    for v in d.variables:
        if not v.name.startswith(prefix) or v.typ != 1:
            continue
        a = v.file_offset + shift
        if src_base <= v.file_offset <= src_base + 0x200:
            out.append((a, v))
    return sorted(out)


def _auto_int(s):
    return int(s, 0)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Таблицы флагов диагностики")
    ap.add_argument("firmwares", nargs="+")
    ap.add_argument("--damos", default="firmware/px5ns03d.dam")
    ap.add_argument("--prefix", default="CLA", choices=sorted(TABLE_BASE))
    ap.add_argument("--changed-only", action="store_true")
    ap.add_argument("--md")
    args = ap.parse_args(argv)

    d = damos.parse(args.damos)
    F = {os.path.basename(p).replace(".bin", ""): fwlib.load(p)
         for p in args.firmwares}
    rows = table_vars(d, args.prefix)
    print("Переменных %s* в таблице: %d, база в целевой прошивке 0x%05X"
          % (args.prefix, len(rows), TABLE_BASE[args.prefix][1]), file=sys.stderr)

    names = list(F)
    short = [n[:11] for n in names]
    hdr = "%-10s %-8s " % ("ИМЯ", "АДРЕС") + " ".join("%-11s" % s for s in short)
    print(hdr + "  ОПИСАНИЕ")
    lines = []
    for a, v in rows:
        vals = [f.u8(a) if f.in_range(a, 1) else -1 for f in F.values()]
        if args.changed_only and len(set(vals)) == 1:
            continue
        line = "%-10s 0x%05X " % (v.name, a) + " ".join("%-11d" % x for x in vals)
        print(line + "  " + v.desc[:56])
        lines.append((v, a, vals))

    if args.md:
        with open(args.md, "w", encoding="utf-8") as fh:
            fh.write("# Флаги диагностики %s*\n\n" % args.prefix)
            fh.write("| Переменная | Адрес | " + " | ".join(names) + " | Описание |\n")
            fh.write("|---" * (len(names) + 3) + "|\n")
            for v, a, vals in lines:
                fh.write("| `%s` | `0x%05X` | %s | %s |\n"
                         % (v.name, a, " | ".join(str(x) for x in vals), v.desc))
        print("\nЗаписано: %s" % args.md, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
