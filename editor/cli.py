#!/usr/bin/env python3
"""
Командная оболочка редактора -- то же ядро, но без окна.

Нужна затем, что ядро должно быть проверяемо без Qt: разбор A2L, адресная
арифметика, пересчёт значений и сравнение прошивок не зависят от
интерфейса и не должны требовать его для запуска.

    python3 -m editor.cli list --a2l F.a2l --bin FW.bin
    python3 -m editor.cli show --a2l F.a2l --bin FW.bin --map KFZWOP
    python3 -m editor.cli diff --a2l F.a2l --bin A.bin --other B.bin
"""

from __future__ import annotations

import argparse
import sys

from . import paths                                         # noqa: F401
import geometry                                             # noqa: E402
import model                                                # noqa: E402
import mapaccess as M                                       # noqa: E402


def _load(a2l_path: str, bin_path: str):
    a2l = model.load(a2l_path)
    buf = open(bin_path, "rb").read()
    am = geometry.detect_addressing(a2l, len(buf))
    return a2l, buf, am


def cmd_list(a) -> int:
    a2l, buf, am = _load(a.a2l, a.bin)
    lays = geometry.resolve_all(a2l, buf, am)
    rows = sorted(lays.values(), key=lambda L: (L.ctype, L.name))
    if a.grep:
        import re
        rx = re.compile(a.grep, re.I)
        rows = [L for L in rows if rx.search(L.name)]
    print("%-24s %-6s %-9s %-9s %-7s %s"
          % ("ИМЯ", "ТИП", "ДАННЫЕ", "РАЗМЕР", "ШИРИНА", "ЕДИНИЦЫ"))
    for L in rows[:a.limit]:
        print("%-24s %-6s 0x%05X   %3dx%-5d %-7s %s%s"
              % (L.name, L.ctype, L.data_off, L.nx, L.ny,
                 ("s" if L.signed else "u") + str(L.width * 8),
                 L.unit, "" if L.editable else "  [только чтение]"))
    print("\nвсего %d, показано %d" % (len(lays), min(len(rows), a.limit)))
    print("адресация: вычитаем 0x%X" % am.subtract)
    return 0


def cmd_show(a) -> int:
    a2l, buf, am = _load(a.a2l, a.bin)
    L = geometry.resolve(a2l, a.map, buf, am)
    print("%s  %s  данные 0x%05X  %dx%d  %s%d  множитель %g  %s"
          % (L.name, L.ctype, L.data_off, L.nx, L.ny,
             "s" if L.signed else "u", L.width * 8, L.factor, L.unit))
    if L.note:
        print("примечание: " + L.note)
    print()
    print(M.fmt_table(buf, L, width=a.width, digits=a.digits))
    return 0


def cmd_diff(a) -> int:
    from compare import compare_files
    a2l, buf, am = _load(a.a2l, a.bin)
    other = open(a.other, "rb").read()
    res = compare_files(a2l, buf, other, am)
    if not res.maps:
        print("карты не отличаются")
    print("%-24s %-9s %-22s %s"
          % ("КАРТА", "ЯЧЕЕК", "ДЕЛЬТА", "ЕДИНИЦЫ"))
    for d in res.maps:
        print("%-24s %3d/%-5d %9.3f .. %-9.3f %s"
              % (d.name, d.changed, d.total, d.dmin, d.dmax, d.unit))
    print("\nизменённых карт: %d, байт всего: %d, вне карт: %d байт"
          % (len(res.maps), res.changed_bytes, res.unmapped_bytes))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Редактор калибровок без окна")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--a2l", required=True)
        p.add_argument("--bin", required=True)

    p = sub.add_parser("list", help="перечислить карты")
    common(p)
    p.add_argument("--grep")
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("show", help="показать карту таблицей")
    common(p)
    p.add_argument("--map", required=True)
    p.add_argument("--width", type=int, default=8)
    p.add_argument("--digits", type=int, default=2)
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("diff", help="сравнить с другой прошивкой")
    common(p)
    p.add_argument("--other", required=True)
    p.set_defaults(fn=cmd_diff)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
