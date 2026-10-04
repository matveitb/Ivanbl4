#!/usr/bin/env python3
"""
codeuse -- КАК код пользуется каждой калибровочной ячейкой.

calref отвечает «кто читает адрес», mapxref -- «какие указатели уходят в
интерполяцию». Этого мало, чтобы проверить ИМЯ. Имя проверяется только
смыслом: если ячейка сравнивается с температурой охлаждающей жидкости, то
"порог по оборотам" ей не подходит, как бы гладко ни сошлось выравнивание.

Поэтому здесь один проход по всему коду, и для каждого обращения к
калибровке записывается ещё и ПАРТНЁР -- то, с чем значение встретилось:

* прямое чтение (`cmpb RL4, 0x1550`, `movbz r5, 0x19d8`) -- партнёр это
  ячейка ОЗУ, загруженная в тот же или во второй регистр сравнения;
* указатель в процедуру интерполяции (`mov r12,#0x1636 / mov r13,#0x206`)
  -- партнёры это входы (`movbz r14, 0xf89e`) и куда лёг результат;
* база таблицы (`mov r12,[r4+#0x616a]`) -- это таблицы указателей, через
  которые читаются KLLAMFA, WDKVLN, WDKKOAN. Раньше их не видел никто:
  дизассемблер считал такую инструкцию двухбайтовой и сбивался.

Регистры прослеживаются в пределах короткого окна, без настоящего
анализа потока данных. Этого хватает: в этой прошивке сравнение почти
всегда стоит через одну-две инструкции от загрузки.

    python3 tools/codeuse.py firmware/FBH3ID60_stok.bin --out out/codeuse.json
    python3 tools/codeuse.py firmware/FBH3ID60_stok.bin --addr 0x19550
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import c166dis                                              # noqa: E402

FLASH = 0x800000
DPP = {0: 0x204, 1: 0x205}                 # калибровка через DPP0/DPP1
PAGES = (0x204, 0x205, 0x206, 0x207)       # калибровочные страницы
CAL_LO, CAL_HI = 0x10000, 0x20000
RAM_LO = 0x8000                            # всё выше -- ОЗУ и регистры

HEX = re.compile(r"^0x([0-9a-f]{1,4})$")
IDX = re.compile(r"^\[r(\d+)\+#0x([0-9a-f]+)\]$")
REG = re.compile(r"^(?:r(\d+)|R([LH])(\d))$")

CMP = {"cmp", "cmpb"}
ARITH = {"add", "addb", "sub", "subb", "addc", "subc"}
LOADS = {"mov", "movb", "movbz", "movbs"}
CALLS = {"calls", "calla", "callr"}
ENDS = {"ret", "rets", "reti", "retp", "jmps", "jmpa"}


def reg_no(op: str):
    m = REG.match(op or "")
    if not m:
        return None
    return int(m.group(1)) if m.group(1) is not None else int(m.group(3))


def cal_addr(val: int, page):
    """16-битный операнд -> смещение в файле, если это калибровка."""
    if page is not None:
        if page not in PAGES:
            return None
        a = (page - 0x200) * 0x4000 + (val & 0x3FFF)
    elif val < 0x8000:
        a = DPP[val >> 14] * 0x4000 + (val & 0x3FFF) - FLASH
    else:
        return None
    return a if CAL_LO <= a < CAL_HI else None


def scan(data: bytes, lo: int = 0x20000, hi: int | None = None) -> dict:
    """
    Вернуть {адрес калибровки: [обращение, ...]}.

    Обращение -- словарь: site, kind ('read'|'ptr'|'table'), text и, если
    нашёлся, partner (ячейка ОЗУ), inputs и dest для указателей.
    """
    hi = hi or len(data)
    dis = c166dis.Disassembler()
    uses: dict = {}
    # регистр -> (род, значение, адрес инструкции)
    #   род: 'ram' (ячейка ОЗУ), 'cal' (калибровка), 'imm', 'call'
    regs: dict = {}
    page, left = None, 0
    pending: list = []          # (вход в uses, номер регистра) -- ждём партнёра

    def add(addr, rec):
        uses.setdefault(addr, []).append(rec)
        return rec

    off = lo
    while off < hi:
        ins = dis.decode(data, off, off + FLASH)
        if ins.length <= 0:
            break
        site = off + FLASH
        mn, ops = ins.mnem, ins.ops
        cur_page = page if left > 0 else None

        if mn in ("extp", "extpr") and len(ops) == 2:
            try:
                page = int(ops[0], 0)
                left = int(ops[1].lstrip("#"), 0)
            except ValueError:
                page, left = None, 0
            off += ins.length
            continue

        # -- обращения к калибровке в операндах
        for i, op in enumerate(ops):
            m = HEX.match(op)
            if m:
                a = cal_addr(int(m.group(1), 16), cur_page)
                if a is not None:
                    rec = add(a, {"site": site, "kind": "read",
                                  "text": ins.text()})
                    # партнёр: регистр по другую сторону сравнения
                    other = ops[1 - i] if len(ops) == 2 else None
                    rn = reg_no(other)
                    if mn in CMP and rn is not None and rn in regs \
                            and regs[rn][0] == "ram":
                        rec["partner"] = regs[rn][1]
                    elif mn in LOADS and i == 1 and rn is not None:
                        regs[rn] = ("cal", a, site)
                        pending.append((rec, rn))
                    elif mn in ARITH and rn is not None and rn in regs \
                            and regs[rn][0] == "ram":
                        rec["partner"] = regs[rn][1]
                    continue
                v = int(m.group(1), 16)
                if v >= RAM_LO and cur_page is None and len(ops) == 2 \
                        and i == 1 and mn in LOADS:
                    rn = reg_no(ops[0])
                    if rn is not None:
                        regs[rn] = ("ram", v, site)
                        # ждавшие партнёра калибровки, загруженные в другой
                        # регистр, получат его на сравнении ниже
                continue
            mi = IDX.match(op)
            if mi:
                a = cal_addr(int(mi.group(2), 16), cur_page)
                if a is not None:
                    add(a, {"site": site, "kind": "table",
                            "index_reg": int(mi.group(1)),
                            "text": ins.text()})

        # -- непосредственные значения: указатели и страницы
        if mn == "mov" and len(ops) == 2 and ops[1].startswith("#"):
            rn = reg_no(ops[0])
            if rn is not None:
                try:
                    regs[rn] = ("imm", int(ops[1][1:], 0), site)
                except ValueError:
                    pass

        # -- сравнение двух регистров: калибровка против ОЗУ
        if mn in CMP and len(ops) == 2:
            ra, rb = reg_no(ops[0]), reg_no(ops[1])
            if ra is not None and rb is not None:
                for x, y in ((ra, rb), (rb, ra)):
                    if regs.get(x, ("",))[0] == "cal" and \
                            regs.get(y, ("",))[0] == "ram":
                        for rec, rn in pending:
                            if rn == x and rec.get("partner") is None \
                                    and site - rec["site"] < 40:
                                rec["partner"] = regs[y][1]

        # -- вызов: снимок аргументов
        if mn in CALLS:
            snap = {r: regs[r] for r in (12, 13, 14, 15) if r in regs
                    and site - regs[r][2] < 48}
            ins_ram = sorted(v[1] for v in snap.values() if v[0] == "ram")
            for lo_r, hi_r in ((12, 13), (14, 15)):
                p = snap.get(lo_r)
                if not p or p[0] != "imm":
                    continue
                q = snap.get(hi_r)
                pg = q[1] if q and q[0] == "imm" and q[1] in PAGES else None
                a = cal_addr(p[1], pg) if pg else (
                    cal_addr(p[1], None) if p[1] >= 0x80 else None)
                if a is None:
                    continue
                dest = _dest(dis, data, off + ins.length)
                add(a, {"site": site, "kind": "ptr", "target": ops[-1],
                        "page": pg, "inputs": ins_ram, "dest": dest,
                        "text": ins.text()})
            # результат вызова лежит в r4; что лежало в регистрах -- забыто
            regs = {4: ("call", ops[-1], site)}
            pending = []
        elif mn in ENDS:
            regs, pending = {}, []

        if left > 0:
            left -= 1
            if left == 0:
                page = None
        off += ins.length
    return uses


def _dest(dis, data, off, look: int = 4):
    for _ in range(look):
        ins = dis.decode(data, off, off + FLASH)
        if ins.length <= 0:
            return None
        if ins.mnem in ("mov", "movb") and len(ins.ops) == 2 \
                and HEX.match(ins.ops[0]) and ins.ops[1] in ("r4", "RL4"):
            v = int(ins.ops[0], 0)
            return v if v >= RAM_LO else None
        off += ins.length
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Как код пользуется калибровкой")
    ap.add_argument("firmware")
    ap.add_argument("--code", nargs=2, type=lambda s: int(s, 0),
                    default=[0x20000, 0x80000])
    ap.add_argument("--addr", type=lambda s: int(s, 0))
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    data = open(a.firmware, "rb").read()
    uses = scan(data, a.code[0], a.code[1])
    kinds = {}
    for v in uses.values():
        for r in v:
            kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    print("ячеек калибровки, к которым обращается код: %d; обращений %s"
          % (len(uses), kinds), file=sys.stderr)
    if a.addr is not None:
        for r in uses.get(a.addr, []):
            extra = ""
            if r.get("partner") is not None:
                extra = "  партнёр 0x%04X" % r["partner"]
            if r["kind"] == "ptr":
                extra = "  входы %s -> %s" % (
                    " ".join("0x%04X" % x for x in r["inputs"]) or "-",
                    ("0x%04X" % r["dest"]) if r.get("dest") else "-")
            print("  0x%06X %-6s %s%s" % (r["site"], r["kind"], r["text"], extra))
    if a.out:
        json.dump({"0x%05X" % k: v for k, v in sorted(uses.items())},
                  open(a.out, "w", encoding="utf-8"), ensure_ascii=False)
        print("записано %s" % a.out, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
