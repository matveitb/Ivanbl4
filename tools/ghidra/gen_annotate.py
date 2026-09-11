#!/usr/bin/env python3
"""
gen_annotate -- собрать Jython-скрипт для Ghidra со всей накопленной разметкой.

На вход идут профиль (profiles/FBH3ID60.json), перенос DAMOS
(out/xfer_FBH3ID60.json) и ссылки кода (out/calref.json). На выходе --
один файл tools/ghidra/Annotate.py, который в Ghidra:

  * ставит метки на все известные калибровочные ячейки с именами из DAMOS
    и комментариями-описаниями;
  * определяет типы: байт, слово, массивы для карт и осей;
  * создаёт ссылки код -> калибровка из calref, чтобы работали
    перекрёстные ссылки там, где адрес собирается из extp и смещения;
  * помечает известные места кода (чтение NMAX, опрос KFLBTS, функции
    холостого хода и отсечки топлива).

Образ грузить как Raw Binary, процессор C166, база 0x800000.

    python3 tools/ghidra/gen_annotate.py
"""

from __future__ import annotations

import json
import os
import sys

BASE = 0x800000
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(ROOT, "tools", "ghidra", "Annotate.py")

CODE_SITES = {
    0x3BF26: "NMAX: чтение отсечки по оборотам 0x14BB4",
    0x3BF1C: "NMAXDV: чтение 0x14BB6",
    0x55B5E: "KFLBTS: опрос карты 0x19636 по оси 0x181F7",
    0x55C34: "маска применения 0x19576, те же оси",
    0x55C84: "цель по лямбде множится на вес 0x98A6",
    0x55AE2: "гейт: счётчик 0x98AF против TVLBTS 0x196F6",
    0x55AF4: "PT1-нарастание веса, постоянная 0x1B8B4",
    0x3E66C: "DNSAH: чтение 0x10EDA",
    0x3E814: "DNSAL: чтение 0x10EDB",
    0x3E938: "TKATSA: чтение 0x10EE3",
    0x517DA: "FRKAP: чтение глобального множителя топлива 0x190CD",
}


def collect(profile: dict, xfer: dict) -> dict[int, tuple[str, str, int, int]]:
    """addr -> (имя, описание, ширина элемента, число элементов)"""
    out: dict[int, tuple[str, str, int, int]] = {}

    def add(addr, name, desc, width=1, count=1):
        if addr is None or not (0 <= addr < 0x80000):
            return
        out.setdefault(addr, (name, desc or "", width, max(1, count)))

    def walk(node, path=""):
        if isinstance(node, dict):
            a = node.get("addr")
            if isinstance(a, str) and a.startswith("0x") and node.get("name"):
                cnt = (node.get("rows", 1) or 1) * (node.get("cols", 1) or 1)
                cnt = max(cnt, node.get("n", 1) or 1)
                add(int(a, 16), node["name"], node.get("desc") or node.get("note", ""),
                    node.get("width", 1) or 1, cnt)
            for k, v in node.items():
                walk(v, path + "/" + k)
        elif isinstance(node, list):
            for v in node:
                walk(v, path)

    walk(profile)
    for v in xfer.get("variables", []):
        if v.get("verify") in ("подтверждена",) or v.get("how") == "структурно":
            cnt = max(1, (v.get("width") or 1) * (v.get("height") or 1))
            add(v["data_addr"], v["name"], v.get("desc", ""), v.get("data_width", 1), cnt)
    return out


def main() -> int:
    prof = json.load(open(os.path.join(ROOT, "profiles", "FBH3ID60.json"), encoding="utf-8"))
    xfer = json.load(open(os.path.join(ROOT, "out", "xfer_FBH3ID60.json"), encoding="utf-8"))
    try:
        refs = json.load(open(os.path.join(ROOT, "out", "calref.json"), encoding="utf-8"))
    except FileNotFoundError:
        refs = {}
        print("нет out/calref.json -- ссылки кода не попадут в скрипт", file=sys.stderr)

    items = collect(prof, xfer)
    flat_refs = {int(k, 16): [int(x, 16) for x in v] for k, v in refs.items()}

    with open(OUT, "w", encoding="utf-8") as f:
        f.write('# Разметка FBH3ID60 для Ghidra. Сгенерирован tools/ghidra/gen_annotate.py.\n')
        f.write('# Образ: Raw Binary, процессор C166, база 0x800000.\n')
        f.write('#\n#@category Firmware\n#@runtime Jython\n\n')
        f.write('from ghidra.program.model.data import ByteDataType, WordDataType, ArrayDataType\n')
        f.write('from ghidra.program.model.symbol import SourceType, RefType\n\n')
        f.write('BASE = 0x%X\n\n' % BASE)
        f.write('NAMES = [\n')
        for a in sorted(items):
            n, d, w, c = items[a]
            f.write('    (0x%05X, %r, %r, %d, %d),\n' % (a, str(n), str(d)[:110], w, c))
        f.write(']\n\n')
        f.write('CODE_SITES = [\n')
        for a in sorted(CODE_SITES):
            f.write('    (0x%05X, %r),\n' % (a, CODE_SITES[a]))
        f.write(']\n\n')
        f.write('REFS = [\n')
        for a in sorted(flat_refs):
            for s in flat_refs[a][:8]:
                f.write('    (0x%05X, 0x%05X),\n' % (s, a))
        f.write(']\n\n')
        f.write(BODY)
    n_refs = sum(min(8, len(v)) for v in flat_refs.values())
    print("записан %s" % OUT)
    print("  именованных ячеек: %d" % len(items))
    print("  мест кода: %d" % len(CODE_SITES))
    print("  ссылок код -> калибровка: %d" % n_refs)
    return 0


BODY = '''
def addr(off):
    return currentProgram.getAddressFactory().getDefaultAddressSpace().getAddress(BASE + off)


def put_data(a, width, count):
    listing = currentProgram.getListing()
    dt = WordDataType() if width == 2 else ByteDataType()
    if count > 1:
        dt = ArrayDataType(dt, count, width)
    try:
        listing.clearCodeUnits(a, a.add(max(0, width * count - 1)), False)
        listing.createData(a, dt)
    except Exception:
        pass


def run():
    sym = currentProgram.getSymbolTable()
    listing = currentProgram.getListing()
    named = 0
    for off, name, desc, width, count in NAMES:
        a = addr(off)
        try:
            sym.createLabel(a, name, SourceType.USER_DEFINED)
            named += 1
        except Exception:
            continue
        if desc:
            listing.setComment(a, 3, desc)
        put_data(a, width, count)
    print("меток на калибровке: %d" % named)

    marked = 0
    for off, note in CODE_SITES:
        a = addr(off)
        listing.setComment(a, 1, note)
        marked += 1
    print("помечено мест кода: %d" % marked)

    made = 0
    for site, target in REFS:
        try:
            currentProgram.getReferenceManager().addMemoryReference(
                addr(site), addr(target), RefType.READ, SourceType.USER_DEFINED, 0)
            made += 1
        except Exception:
            continue
    print("создано ссылок код -> калибровка: %d" % made)


run()
'''


if __name__ == "__main__":
    sys.exit(main())
