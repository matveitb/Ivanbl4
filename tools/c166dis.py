#!/usr/bin/env python3
"""
c166dis -- дизассемблер Infineon C166 / C167.

Таблица инструкций НЕ написана по памяти: она порождается из файла
tools/c166/c166.sinc -- это Sleigh-спецификация из процессорного модуля
C166 для Ghidra, то есть машиночитаемое описание системы команд.
Поэтому опкоды, длины и форматы операндов соответствуют спецификации,
а не воспоминаниям об архитектуре.

Правильность проверяется на эталонных векторах того же модуля
(tests/headless/cases/*/expected.disasm) -- см. tests/test_c166dis.py.

Раскладка токенов (из c166.sinc):

  instr_16 (первое слово, little-endian):
      op=(0,7)  op0003=(0,3)  cond0407=(4,7)  bit_q_0407=(4,7)
      rwm/rbm/reg_low=(8,11)  rwn/rbn/cond/data4/reg_high=(12,15)
      rwi=(8,9)  data3=(8,10)  idx_mode=(10)  idx_imm=(11)
      irang2=(12,13)  irang_mode=(14,15)
      reg=(8,15)  rel=(8,15) со знаком  urel=(8,15)  trap7=(9,15)
      bitoff=(8,14)  bitoff_high_bit=(15)

  instr_16_2 (второе слово):
      caddr/page/seg/data16/mem=(0,15)  rel_32=(0,7) со знаком
      z=(4,7)  z_2=(0,3)  bit_z=(8,11)  bit_q=(12,15)  mask8=(8,15)

Использование:
    python3 tools/c166dis.py firmware/FBH3ID60_stok.bin --at 0x3B5F0 --count 20
    python3 tools/c166dis.py firmware/FBH3ID60_stok.bin --range 0x3B5F0 0x3B640
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field

SINC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "c166", "c166.sinc")

# Поля второго слова -- по ним определяется длина инструкции (4 байта)
SECOND_TOKEN_FIELDS = {
    "caddr", "page", "seg", "data16", "mem", "z", "z_high_bit", "z_2",
    "bitaddr_z", "bit_z", "bit_q", "mask8", "rel_32",
}

# Подконструкторы, читающие второе слово
SECOND_TOKEN_SUBS = {
    "DataImmW", "DataImmB", "LongMemAddrW", "LongMemAddrB",
    "absCaddr", "relSeg", "rel1623", "BitoffAddrZ",
}

CONDITIONS = {
    0x0: "cc_UC", 0x1: "cc_NET", 0x2: "cc_EQ", 0x3: "cc_NE", 0x4: "cc_V",
    0x5: "cc_NV", 0x6: "cc_N", 0x7: "cc_NN", 0x8: "cc_C", 0x9: "cc_NC",
    0xA: "cc_SGT", 0xB: "cc_SLE", 0xC: "cc_SLT", 0xD: "cc_SGE",
    0xE: "cc_UGT", 0xF: "cc_ULE",
}


@dataclass
class Ctor:
    mnem: str
    display: str
    constraints: dict          # поле -> значение
    operands: list             # имена подконструкторов/полей по порядку
    length: int                # 2 или 4
    raw_pattern: str = ""
    rwn_eq_rwm: bool = False   # подконструктор RWnRWmEqual: требует rwn == rwm


# --------------------------------------------------------------------------
# Разбор спецификации
# --------------------------------------------------------------------------

def _sub_uses_second_token(src: str) -> dict:
    """Определить, какие подконструкторы читают второе слово."""
    uses = dict.fromkeys(SECOND_TOKEN_SUBS, True)
    for m in re.finditer(r'^([A-Za-z_]\w*):\s*[^\n]*?\s+is\s+([^{\[]+)', src, re.M):
        name, pat = m.group(1), m.group(2)
        if name in uses:
            continue
        uses[name] = bool(SECOND_TOKEN_FIELDS & set(re.findall(r'\w+', pat)))
    return uses


def load_table(path: str = SINC) -> list[Ctor]:
    src = open(path, encoding="utf-8", errors="replace").read()
    sub_second = _sub_uses_second_token(src)

    table: list[Ctor] = []
    for m in re.finditer(r'^:(\S+)([^\n]*?)\s+is\s+([^{]+?)\s*\{', src, re.M):
        mnem, disp, pat = m.group(1), m.group(2).strip(), m.group(3).strip()

        cons = {}
        for f, v in re.findall(r'(\w+)\s*=\s*(0x[0-9a-fA-F]+|\d+)', pat):
            cons[f] = int(v, 0)

        names = set(re.findall(r'[A-Za-z_]\w*', pat))
        # Строка отображения -- это шаблон: скобки, +, - и # значимы
        # (например ":mov rwn, [rwm]" против ":mov rwn, RwmIndW").
        operands = [t for t in re.findall(r'[A-Za-z_]\w*', disp) if t in names]
        rwn_eq_rwm = "RWnRWmEqual" in names

        four = (";" in pat) or ("op_32" in cons)
        if not four:
            for t in names:
                if sub_second.get(t) or t in SECOND_TOKEN_FIELDS:
                    four = True
                    break
        c = Ctor(mnem=mnem, display=disp, constraints=cons,
                 operands=operands, length=4 if four else 2, raw_pattern=pat)
        c.rwn_eq_rwm = rwn_eq_rwm
        table.append(c)
    return table


# --------------------------------------------------------------------------
# Извлечение полей
# --------------------------------------------------------------------------

def _fields(w0: int, w1: int | None) -> dict:
    f = {
        "op": w0 & 0xFF,
        "op0003": w0 & 0xF,
        "cond0407": (w0 >> 4) & 0xF,
        "bit_q_0407": (w0 >> 4) & 0xF,
        "op_noop": w0,
        "rwm": (w0 >> 8) & 0xF, "rbm": (w0 >> 8) & 0xF,
        "reg_low": (w0 >> 8) & 0xF, "reg_low_b": (w0 >> 8) & 0xF,
        "rwn": (w0 >> 12) & 0xF, "rbn": (w0 >> 12) & 0xF,
        "cond": (w0 >> 12) & 0xF, "data4": (w0 >> 12) & 0xF,
        "reg_high": (w0 >> 12) & 0xF,
        "rwi": (w0 >> 8) & 0x3,
        "data3": (w0 >> 8) & 0x7,
        "idx_mode": (w0 >> 10) & 1,
        "idx_imm": (w0 >> 11) & 1,
        "irang2": (w0 >> 12) & 0x3,
        "irang_mode": (w0 >> 14) & 0x3,
        "reg": (w0 >> 8) & 0xFF,
        "urel": (w0 >> 8) & 0xFF,
        "trap7": (w0 >> 9) & 0x7F,
        "bitoff": (w0 >> 8) & 0x7F,
        "bitoff_high_bit": (w0 >> 15) & 1,
    }
    r = (w0 >> 8) & 0xFF
    f["rel"] = r - 256 if r >= 128 else r
    if w1 is not None:
        f.update({
            "caddr": w1, "page": w1, "seg": w1, "data16": w1, "mem": w1,
            "z": (w1 >> 4) & 0xF, "z_2": w1 & 0xF,
            "z_high_bit": (w1 >> 7) & 1,
            "bitaddr_z": w1 & 0x7F,
            "bit_z": (w1 >> 8) & 0xF, "bit_q": (w1 >> 12) & 0xF,
            "mask8": (w1 >> 8) & 0xFF,
        })
        rr = w1 & 0xFF
        f["rel_32"] = rr - 256 if rr >= 128 else rr
        f["op_32"] = w0 | (w1 << 16)
    return f


# --------------------------------------------------------------------------
# Форматирование операндов
# --------------------------------------------------------------------------

def _short_mem(reg: int, byte: bool) -> str:
    """ShortMemAddr: reg_high==0xF -> регистр общего назначения, иначе SFR."""
    if (reg >> 4) == 0xF:
        n = reg & 0xF
        return ("RL%d" % (n // 2)) if byte and n % 2 == 0 else \
               ("RH%d" % (n // 2)) if byte else ("r%d" % n)
    return "0x%04x" % (0xFE00 + 2 * reg)


def _fmt_operand(name: str, f: dict, addr: int, length: int) -> str:
    if name in ("rwn", "rwm"):
        return "r%d" % f[name]
    if name in ("rbn", "rbm"):
        n = f[name]
        return ("RL%d" if n % 2 == 0 else "RH%d") % (n // 2)
    if name == "ShortMemAddrW":
        return _short_mem(f["reg"], False)
    if name == "ShortMemAddrB":
        return _short_mem(f["reg"], True)
    if name in ("LongMemAddrW", "LongMemAddrB"):
        return "0x%04x" % f["mem"]
    if name in ("DataImmW", "DataImmB"):
        return "#0x%x" % f["data16"]
    if name in ("data4", "data3", "irang2", "mask8", "trap7"):
        return "0x%x" % f[name]
    if name in ("IndexImmW", "IndexImmB"):
        if f["idx_imm"] == 0:
            return "#0x%x" % f["data3"]
        return "[r%d%s]" % (f["rwi"], "+" if f["idx_mode"] else "")
    if name in ("RwmIndW", "RwmIndB"):
        return "[r%d]" % f["rwm"]
    if name in ("RwmIndWP", "RwmIndBP"):
        return "[r%d+]" % f["rwm"]
    if name in ("RwmIndWM", "RwmIndBM"):
        return "[-r%d]" % f["rwm"]
    if name in ("RwnIndWP", "RwnIndBP"):
        return "[r%d+]" % f["rwn"]
    if name in ("RwnIndW", "RwnIndB"):
        return "[r%d]" % f["rwn"]
    if name in ("RwmIndWOff", "RwmIndBOff"):
        return "[r%d+#0x%x]" % (f["rwm"], f.get("data16", 0))
    if name == "BitoffAddr":
        if f["reg_high"] == 0xF:
            return "r%d" % f["reg_low"]
        base = 0xFF00 if f["bitoff_high_bit"] else 0xFD00
        return "0x%04x" % (base + 2 * f["bitoff"])
    if name == "BitoffAddrZ":
        base = 0xFF00 if f.get("z_high_bit") else 0xFD00
        return "0x%04x" % (base + 2 * f.get("bitaddr_z", 0))
    if name in ("bit_q", "bit_z", "bit_q_0407"):
        return "%d" % f[name]
    # Составные операнды префиксов EXTP/EXTS/EXTR (см. SetExtp и др. в c166.sinc):
    # SetAtomic печатает #(irang2+1) -- число защищённых от прерывания команд.
    if name in ("SetAtomic", "SetExtr"):
        return "#0x%x" % (f["irang2"] + 1)
    if name == "SetExtp":
        return "0x%x, #0x%x" % (f.get("page", 0), f["irang2"] + 1)
    if name == "SetExts":
        return "0x%x, #0x%x" % (f.get("seg", 0), f["irang2"] + 1)
    if name in ("SetExtpInd", "SetExtsInd"):
        return "r%d, #0x%x" % (f["rwm"], f["irang2"] + 1)
    if name in ("cc1215",):
        return CONDITIONS.get(f["cond"], "cc_%X" % f["cond"])
    if name == "cc0407":
        return CONDITIONS.get(f["cond0407"], "cc_%X" % f["cond0407"])
    if name == "rel0815":
        return "0x%06x" % ((addr + length + 2 * f["rel"]) & 0xFFFFFF)
    if name == "rel1623":
        return "0x%06x" % ((addr + length + 2 * f["rel_32"]) & 0xFFFFFF)
    if name == "absCaddr":
        return "0x%06x" % ((addr & 0xFF0000) | f["caddr"])
    if name == "relSeg":
        return "0x%06x" % ((f["urel"] << 16) | f["caddr"])
    if name == "trapAddr":
        return "#0x%x" % (f["trap7"] << 2)
    if name in f:
        return "0x%x" % f[name]
    return name


def render(disp: str, f: dict, addr: int, length: int, names: set) -> str:
    """
    Отрисовать строку отображения Sleigh как шаблон: идентификаторы-операнды
    заменяются значениями, остальные символы (скобки, +, -, #) сохраняются.
    Символ ^ в Sleigh -- склейка без пробела.
    """
    out = []
    i = 0
    while i < len(disp):
        ch = disp[i]
        if ch == "^":
            i += 1
            continue
        if ch == '"':
            j = disp.find('"', i + 1)
            if j < 0:
                j = len(disp)
            out.append(disp[i + 1:j])
            i = j + 1
            continue
        if ch.isalpha() or ch == "_":
            j = i
            while j < len(disp) and (disp[j].isalnum() or disp[j] == "_"):
                j += 1
            word = disp[i:j]
            out.append(_fmt_operand(word, f, addr, length) if word in names else word)
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


@dataclass
class Insn:
    addr: int
    length: int
    mnem: str
    ops: list
    raw: bytes
    ctor: Ctor | None = None

    def text(self) -> str:
        return ("%-7s %s" % (self.mnem, ", ".join(self.ops))).rstrip()

    @property
    def is_call(self) -> bool:
        return self.mnem in ("calla", "calls", "callr", "calli", "pcall")

    @property
    def is_ret(self) -> bool:
        return self.mnem in ("ret", "rets", "reti", "retp", "retn")

    @property
    def is_jump(self) -> bool:
        return self.mnem in ("jmpa", "jmps", "jmpr", "jmpi")


class Disassembler:
    def __init__(self, table: list[Ctor] | None = None) -> None:
        self.table = table if table is not None else load_table()
        self.by_op: dict[int, list[Ctor]] = {}
        self.no_op: list[Ctor] = []
        for c in self.table:
            if "op" in c.constraints:
                self.by_op.setdefault(c.constraints["op"], []).append(c)
            else:
                self.no_op.append(c)
        # более специфичные (больше ограничений) проверяем первыми
        for lst in self.by_op.values():
            lst.sort(key=lambda c: -len(c.constraints))
        self.no_op.sort(key=lambda c: -len(c.constraints))

    def decode(self, data: bytes, off: int, addr: int | None = None) -> Insn:
        if addr is None:
            addr = off
        if off >= len(data):
            return Insn(addr, 0, ".end", [], b"")
        if off + 2 > len(data):
            return Insn(addr, 1, ".byte", ["0x%02x" % data[off]], data[off:off + 1])
        w0 = data[off] | (data[off + 1] << 8)
        w1 = None
        if off + 4 <= len(data):
            w1 = data[off + 2] | (data[off + 3] << 8)
        f = _fields(w0, w1)

        for c in self.by_op.get(w0 & 0xFF, []) + self.no_op:
            if c.length == 4 and w1 is None:
                continue
            ok = True
            for k, v in c.constraints.items():
                if f.get(k) != v:
                    ok = False
                    break
            if not ok:
                continue
            if c.rwn_eq_rwm and f["rwn"] != f["rwm"]:
                continue
            names = set(re.findall(r'[A-Za-z_]\w*', c.raw_pattern))
            txt = render(c.display, f, addr, c.length, names)
            ops = [p.strip() for p in txt.split(",")] if txt.strip() else []
            return Insn(addr, c.length, c.mnem, ops,
                        data[off:off + c.length], c)

        return Insn(addr, 2, ".word", ["0x%04x" % w0], data[off:off + 2])


def disassemble(data: bytes, start: int, end: int, base: int = 0,
                dis: Disassembler | None = None):
    dis = dis or Disassembler()
    off = start
    while off < min(end, len(data)):
        ins = dis.decode(data, off, base + off)
        yield ins
        off += max(1, ins.length)


def _auto_int(s: str) -> int:
    return int(s, 0)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Дизассемблер C166/C167")
    ap.add_argument("firmware")
    ap.add_argument("--at", type=_auto_int)
    ap.add_argument("--count", type=int, default=24)
    ap.add_argument("--range", nargs=2, type=_auto_int, metavar=("START", "END"))
    ap.add_argument("--base", type=_auto_int, default=0x800000,
                    help="адрес отображения образа (по умолчанию 0x800000)")
    ap.add_argument("--raw", action="store_true", help="печатать байты")
    args = ap.parse_args(argv)

    data = open(args.firmware, "rb").read()
    dis = Disassembler()
    print("Инструкций в таблице: %d (из c166.sinc)" % len(dis.table), file=sys.stderr)

    if args.range:
        start, end = args.range
    elif args.at is not None:
        start, end = args.at, args.at + args.count * 4
    else:
        start, end = 0x10000, 0x10000 + args.count * 4

    n = 0
    for ins in disassemble(data, start, end, base=args.base, dis=dis):
        raw = " ".join("%02X" % b for b in ins.raw) if args.raw else ""
        print("%06X  %-12s %s" % (ins.addr, raw, ins.text()))
        n += 1
        if args.at is not None and n >= args.count:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
