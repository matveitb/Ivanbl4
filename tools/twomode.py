#!/usr/bin/env python3
"""
twomode -- двухрежимная прошивка FBH3ID60 / Bosch M7.9.7: обычный и спорт.

Переключение педалью: зажигание включено, мотор не заведён, машина стоит,
педаль газа в пол и держать TTOGGLE (3 с) -- режим сменился. Отпустить
педаль и выжать снова -- сменился обратно. Режим не запоминается: ОЗУ
0x8000..0xAD3F блок обнуляет при каждом пуске (таблица 0x148E0), каждая
поездка начинается с обычного.

В спорте подменяются четыре величины, каждая одним перехватом в коде:

  зажигание   KFZW (0x109F5) -> KFZW2 (0x10AB5). KFZW2 те же 12x16 на тех же
              осях, и код её не читает нигде: место под карту уже было.
  отсечка     NMAX (0x14BB4) -> NMAX_SP. Аварийные пределы (NMAXDV при
              отказе скорости, NMXDKPU) выбираются раньше перехвата и
              остаются как были.
  лаунч       только в спорте: скорость < VLAU и дроссель >= WDKLAU ->
              предел оборотов NLAUNCH тем же штатным ограничителем.
  смесь       KLLAMFA (полный газ, обе кривые варианта 0xF87A) ->
              KLLAMFA_SP, своя пара кривых с той же раскладкой.

Перехваты (адреса в образе, исходные байты проверяются перед заменой):

  0x30550  calls 0x83be6e        задача 10 мс: ограничитель оборотов.
                                 Стала: логика режима, затем jmps туда же.
  0x3E3D2  mov r12, #0x9f5       выбор KFZW
  0x3BF26  mov r4, NMAX          значение отсечки
  0x551D0  mov r12/r13, [r4+#0x616a/c]   указатель на KLLAMFA

Код ложится в пустую флеш 0x6C000 (сегмент 0x86), калибровки режима -- в
пустую 0x17000 (страница 0x205, видна через DPP1 как 0x7000). Обе области
пусты во всех известных прошивках FBH3ID60 и закрыты контрольными суммами,
которые пересчитываются тут же.

ОЗУ режима: 0x8400 -- состояние (бит 0 спорт, бит 1 ждём отпускания педали),
0x8402 -- счётчик удержания. Участок 0x8120..0x87FF код не трогает ни одной
инструкцией и ни одним указателем; стек -- в XRAM от 0xE7FE.

    python3 tools/twomode.py show    прошивка.bin
    python3 tools/twomode.py install прошивка.bin -o двухрежимка.bin
    python3 tools/twomode.py set     двухрежимка.bin -o новая.bin --nlaunch 4200
    python3 tools/twomode.py remove  двухрежимка.bin -o обычная.bin
"""

from __future__ import annotations

import argparse
import re
import struct
import sys

import bosch_csum
import fwlib

# -- где что лежит --------------------------------------------------------

CODE_OFF = 0x6C000            # файл; в адресах блока 0x86C000
CODE_END = 0x6D000
CODE_BASE = 0x800000

CAL_OFF = 0x17000             # файл; через DPP1 (страница 0x205) это 0x7000
CAL_END = 0x17100
CAL_LOGICAL = 0x7000
CAL_PAGE = 0x205              # 0x205 * 0x4000 = 0x814000

RAM_STATE = 0x8400
RAM_COUNT = 0x8402

KFZW = 0x109F5
KFZW2 = 0x10AB5
KFZW_SIZE = 12 * 16
NMAX = 0x14BB4
KLLAMFA_PTR = 0x1616A         # {смещение, страница} x2, вариант по 0xF87A
CURVE_LEN = 13                # счётчик 6, ось 6, значения 6

SIGNATURE = b"2MOD"
VERSION = 1

# Калибровки режима: имя, смещение от CAL_OFF, тип, множитель, единица, смысл
CAL = [
    ("NMAX_SP", 0x08, "H", 0.25,     "об/мин", "отсечка в спорте"),
    ("NLAUNCH", 0x0A, "H", 0.25,     "об/мин", "предел оборотов лаунча"),
    ("NSTOP",   0x0C, "H", 0.25,     "об/мин", "ниже -- мотор стоит, можно переключать"),
    ("TTOGGLE", 0x0E, "H", 0.01,     "с",      "сколько держать педаль для переключения"),
    ("WDKTOG",  0x10, "B", 0.390625, "%",      "педаль не меньше -- считается выжатой"),
    ("WDKREL",  0x11, "B", 0.390625, "%",      "педаль меньше -- считается отпущенной"),
    ("WDKLAU",  0x12, "B", 0.390625, "%",      "лаунч: дроссель не меньше"),
    ("VLAU",    0x13, "B", 1.25,     "км/ч",   "лаунч: скорость меньше (0 -- лаунч выключен)"),
]
CAL_BY_NAME = {c[0]: c for c in CAL}
KLLAMFA_SP_PTR = 0x14         # от CAL_OFF: {смещение, страница} x2
KLLAMFA_SP = (0x1C, 0x1C + CURVE_LEN)   # от CAL_OFF: две кривые подряд
CAL_USED = 0x1C + 2 * CURVE_LEN

DEFAULTS = {
    "NLAUNCH": 4000,
    "NSTOP": 20,
    "TTOGGLE": 3.0,
    "WDKTOG": 80.0,
    "WDKREL": 20.0,
    "WDKLAU": 60.0,
    "VLAU": 5.0,
}


def cal_logical(rel: int) -> int:
    return CAL_LOGICAL + rel


# -- код ------------------------------------------------------------------
#
# Пишется текстом в том виде, в каком его печатает tools/c166dis.py: тесты
# дизассемблируют собранные байты и сверяют текст строка в строку, так что
# кодировку проверяет дизассемблер, построенный из спецификации Ghidra,
# а не этот же файл.

def _c(rel: int) -> str:
    return "0x%04x" % cal_logical(rel)


def _cal(name: str) -> str:
    return _c(CAL_BY_NAME[name][1])


SOURCE = f"""
; ---- задача 10 мс: переключение педалью, затем штатный ограничитель
tm_task:
    mov     r4, 0xf8a0              ; обороты x4
    cmp     r4, {_cal('NSTOP')}
    jmpr    cc_NC, t_clr            ; мотор крутится -- не переключаем
    movb    RL4, 0x881d             ; скорость, 1.25 км/ч
    cmpb    RL4, #0x0
    jmpr    cc_NE, t_clr            ; машина катится -- не переключаем
    movb    RL4, 0x89f3             ; дроссель, 0.390625 %
    cmpb    RL4, {_cal('WDKREL')}
    jmpr    cc_NC, t_hi
    mov     r5, 0x{RAM_STATE:04x}
    jnb     r5.1, t_clr
    bclr    r5.1                    ; педаль отпущена -- можно снова
    mov     0x{RAM_STATE:04x}, r5
    jmpr    cc_UC, t_clr
t_hi:
    cmpb    RL4, {_cal('WDKTOG')}
    jmpr    cc_C, t_clr             ; между порогами -- не считаем
    mov     r5, 0x{RAM_STATE:04x}
    jb      r5.1, t_clr             ; уже переключили этим нажатием
    mov     r4, 0x{RAM_COUNT:04x}
    add     r4, #0x1
    mov     0x{RAM_COUNT:04x}, r4
    cmp     r4, {_cal('TTOGGLE')}
    jmpr    cc_C, t_end
    jb      r5.0, t_off
    bset    r5.0
    jmpr    cc_UC, t_set
t_off:
    bclr    r5.0
t_set:
    bset    r5.1
    mov     0x{RAM_STATE:04x}, r5
t_clr:
    mov     0x{RAM_COUNT:04x}, ZEROS
t_end:
    jmps    0x83be6e

; ---- зажигание: вместо mov r12, #0x9f5
tm_zw:
    mov     r12, 0x{RAM_STATE:04x}
    jnb     r12.0, zw_n
    mov     r12, #0x{KFZW2 - 0x10000:x}
    rets
zw_n:
    mov     r12, #0x{KFZW - 0x10000:x}
    rets

; ---- отсечка и лаунч: вместо mov r4, NMAX
tm_nmax:
    mov     r4, 0x{RAM_STATE:04x}
    jnb     r4.0, nm_n
    movb    RL4, 0x881d
    cmpb    RL4, {_cal('VLAU')}
    jmpr    cc_NC, nm_s             ; едем -- спортивная отсечка
    movb    RL4, 0x89f3
    cmpb    RL4, {_cal('WDKLAU')}
    jmpr    cc_C, nm_s              ; газ не в пол -- спортивная отсечка
    mov     r4, {_cal('NLAUNCH')}
    rets
nm_s:
    mov     r4, {_cal('NMAX_SP')}
    rets
nm_n:
    mov     r4, 0x{NMAX - 0x10000:04x}
    rets

; ---- смесь полного газа: вместо mov r12/r13, [r4+#0x616a/c]
tm_lam:
    mov     r12, 0x{RAM_STATE:04x}
    jnb     r12.0, lam_n
    mov     r12, [r4+#{_c(KLLAMFA_SP_PTR)}]
    mov     r13, [r4+#{_c(KLLAMFA_SP_PTR + 2)}]
    rets
lam_n:
    mov     r12, [r4+#0x616a]
    mov     r13, [r4+#0x616c]
    rets
"""

# место, исходные байты, чем заменить (метка, хвост из nop)
HOOKS = [
    (0x30550, bytes.fromhex("da836ebe"), "tm_task", 0, "задача 10 мс -> логика режима"),
    (0x3E3D2, bytes.fromhex("e6fcf509"), "tm_zw", 0, "выбор KFZW"),
    (0x3BF26, bytes.fromhex("f2f4b44b"), "tm_nmax", 0, "значение NMAX"),
    (0x551D0, bytes.fromhex("d4c46a61d4d46c61"), "tm_lam", 2, "указатель KLLAMFA"),
]


# -- ассемблер на те формы, что нужны выше ----------------------------------

REG_ALIAS = {"ZEROS": 0x8E, "ONES": 0x8F}
COND = {"cc_UC": 0x0, "cc_EQ": 0x2, "cc_NE": 0x3, "cc_C": 0x8, "cc_NC": 0x9}


def _num(s: str) -> int:
    return int(s, 0)


def _rw(s: str) -> int:
    m = re.fullmatch(r"r(\d+)", s)
    if not m or int(m.group(1)) > 15:
        raise ValueError("не регистр: %s" % s)
    return int(m.group(1))


def _reg8(s: str) -> int:
    """Короткий адрес регистра для форм reg, mem: 0xF0+n или ZEROS/ONES."""
    if s in REG_ALIAS:
        return REG_ALIAS[s]
    m = re.fullmatch(r"R([LH])(\d)", s)
    if m:
        return 0xF0 + int(m.group(2)) * 2 + (m.group(1) == "H")
    return 0xF0 + _rw(s)


def _w(v: int) -> bytes:
    return struct.pack("<H", v & 0xFFFF)


def _bit(s: str) -> tuple[int, int]:
    reg, q = s.split(".")
    return 0xF0 + _rw(reg), int(q)


def _rel(target: int, after: int) -> int:
    d = target - after
    if d % 2 or not -256 <= d <= 254:
        raise ValueError("переход вне досягаемости: %d" % d)
    return (d // 2) & 0xFF


def _encode(mn: str, ops: list[str], addr: int, labels: dict[str, int]) -> bytes:
    def target(s: str) -> int:
        return labels[s] if s in labels else _num(s)

    o = ops
    if mn == "rets":
        return b"\xdb\x00"
    if mn == "nop":
        return b"\xcc\x00"
    if mn in ("calls", "jmps"):
        t = target(o[0])
        return bytes([0xDA if mn == "calls" else 0xFA, t >> 16]) + _w(t)
    if mn == "jmpr":
        return bytes([(COND[o[0]] << 4) | 0x0D, _rel(target(o[1]), addr + 2)])
    if mn in ("jb", "jnb"):
        bo, q = _bit(o[0])
        return bytes([0x8A if mn == "jb" else 0x9A, bo,
                      _rel(target(o[1]), addr + 4), q << 4])
    if mn in ("bset", "bclr"):
        bo, q = _bit(o[0])
        return bytes([(q << 4) | (0x0F if mn == "bset" else 0x0E), bo])
    if mn == "add" and o[1].startswith("#") and _num(o[1][1:]) < 8:
        return bytes([0x08, (_rw(o[0]) << 4) | _num(o[1][1:])])
    if mn == "mov":
        m = re.fullmatch(r"\[r(\d+)\+#(0x[0-9a-f]+)\]", o[1])
        if m:
            return bytes([0xD4, (_rw(o[0]) << 4) | int(m.group(1))]) + _w(_num(m.group(2)))
        if o[1].startswith("#"):
            return bytes([0xE6, _reg8(o[0])]) + _w(_num(o[1][1:]))
        if o[0].startswith("0x"):
            return bytes([0xF6, _reg8(o[1])]) + _w(_num(o[0]))
        return bytes([0xF2, _reg8(o[0])]) + _w(_num(o[1]))
    if mn == "movb":
        return bytes([0xF3, _reg8(o[0])]) + _w(_num(o[1]))
    if mn == "cmp":
        return bytes([0x42, _reg8(o[0])]) + _w(_num(o[1]))
    if mn == "cmpb":
        if o[1].startswith("#"):
            return bytes([0x47, _reg8(o[0]), _num(o[1][1:]), 0x00])
        return bytes([0x43, _reg8(o[0])]) + _w(_num(o[1]))
    raise ValueError("форма не поддержана: %s %s" % (mn, ", ".join(o)))


def parse(source: str) -> list[tuple[str | None, str, list[str]]]:
    """Строки -> (метка, мнемоника, операнды). Метка стоит отдельной строкой."""
    out, label = [], None
    for raw in source.splitlines():
        line = raw.split(";", 1)[0].strip()
        if not line:
            continue
        if line.endswith(":"):
            label = line[:-1]
            continue
        parts = line.split(None, 1)
        ops = [p.strip() for p in parts[1].split(",")] if len(parts) > 1 else []
        out.append((label, parts[0], ops))
        label = None
    return out


def assemble(source: str, base: int) -> tuple[bytes, dict[str, int], list[tuple[int, str]]]:
    """Два прохода: длины, потом байты. Вернуть код, метки и листинг."""
    items = parse(source)
    labels: dict[str, int] = {}
    addr = base
    dummy = {lab: base for lab, _, _ in items if lab}
    for lab, mn, ops in items:
        if lab:
            labels[lab] = addr
        addr += len(_encode(mn, ops, addr, {**dummy, **labels}))
    code, listing, addr = bytearray(), [], base
    for lab, mn, ops in items:
        b = _encode(mn, ops, addr, labels)
        text = mn + (" " + ", ".join("0x%x" % labels[x] if x in labels else x
                                      for x in ops) if ops else "")
        listing.append((addr, text))
        code += b
        addr += len(b)
    return bytes(code), labels, listing


def build() -> tuple[bytes, dict[str, int], list[tuple[int, str]]]:
    return assemble(SOURCE, CODE_BASE + CODE_OFF)


# -- калибровки ---------------------------------------------------------------

def _cal_get(data: bytes, name: str) -> float:
    _, rel, fmt, k, _, _ = CAL_BY_NAME[name]
    return struct.unpack_from("<" + fmt, data, CAL_OFF + rel)[0] * k


def _cal_raw(name: str, value: float) -> int:
    _, _, fmt, k, unit, _ = CAL_BY_NAME[name]
    raw = int(round(value / k))
    hi = 0xFFFF if fmt == "H" else 0xFF
    if not 0 <= raw <= hi:
        raise ValueError("%s = %g %s вне диапазона" % (name, value, unit))
    return raw


def _cal_put(buf: bytearray, name: str, value: float) -> None:
    _, rel, fmt, _, _, _ = CAL_BY_NAME[name]
    struct.pack_into("<" + fmt, buf, CAL_OFF + rel, _cal_raw(name, value))


def _validate(buf: bytes) -> list[str]:
    g = {c[0]: _cal_get(buf, c[0]) for c in CAL}
    problems = []
    if g["NLAUNCH"] > g["NMAX_SP"]:
        problems.append("NLAUNCH %g выше NMAX_SP %g" % (g["NLAUNCH"], g["NMAX_SP"]))
    if g["WDKREL"] >= g["WDKTOG"]:
        problems.append("WDKREL %g не ниже WDKTOG %g" % (g["WDKREL"], g["WDKTOG"]))
    if g["TTOGGLE"] <= 0:
        problems.append("TTOGGLE должна быть больше нуля")
    if g["NSTOP"] <= 0:
        problems.append("NSTOP должна быть больше нуля, иначе переключение не сработает")
    return problems


# -- установка ----------------------------------------------------------------

class TwoModeError(Exception):
    pass


def installed(data: bytes) -> bool:
    return data[CAL_OFF:CAL_OFF + 4] == SIGNATURE


def _check_pristine(data: bytes) -> None:
    if len(data) != 0x80000:
        raise TwoModeError("ожидается образ 512 КБ, а здесь %d байт" % len(data))
    for off, orig, label, _, what in HOOKS:
        got = data[off:off + len(orig)]
        if got != orig:
            raise TwoModeError("0x%05X (%s): ожидалось %s, в файле %s -- это не тот код"
                               % (off, what, orig.hex(" "), got.hex(" ")))
    for lo, hi, what in ((CODE_OFF, CODE_END, "под код"), (CAL_OFF, CAL_END, "под калибровки")):
        if any(b != 0xFF for b in data[lo:hi]):
            raise TwoModeError("область 0x%05X..0x%05X %s не пуста" % (lo, hi, what))


def _hook_bytes(label: str, pad: int, labels: dict[str, int]) -> bytes:
    t = labels[label]
    return bytes([0xDA, t >> 16]) + _w(t) + b"\xcc\x00" * pad


def install(data: bytes, params: dict[str, float] | None = None,
            copy_kfzw: bool = True) -> bytearray:
    """Поставить двухрежимку. Спорт-карты -- копии обычных, кроме лаунча."""
    _check_pristine(data)
    code, labels, _ = build()
    buf = bytearray(data)
    if len(code) > CODE_END - CODE_OFF:
        raise TwoModeError("код не влез")
    buf[CODE_OFF:CODE_OFF + len(code)] = code

    struct.pack_into("<4sHH", buf, CAL_OFF, SIGNATURE, VERSION, 0)
    vals = dict(DEFAULTS)
    vals["NMAX_SP"] = struct.unpack_from("<H", data, NMAX)[0] * 0.25
    vals.update(params or {})
    for name, value in vals.items():
        _cal_put(buf, name, value)

    # своя пара кривых KLLAMFA, начинаем с копии штатной
    ptrs = struct.unpack_from("<4H", data, KLLAMFA_PTR)
    for i, rel in enumerate(KLLAMFA_SP):
        src = ptrs[2 * i + 1] * 0x4000 + ptrs[2 * i] - 0x800000
        buf[CAL_OFF + rel:CAL_OFF + rel + CURVE_LEN] = data[src:src + CURVE_LEN]
        struct.pack_into("<HH", buf, CAL_OFF + KLLAMFA_SP_PTR + 4 * i,
                         (CAL_OFF + rel) - (CAL_PAGE * 0x4000 - 0x800000), CAL_PAGE)

    if copy_kfzw:
        buf[KFZW2:KFZW2 + KFZW_SIZE] = data[KFZW:KFZW + KFZW_SIZE]

    for off, _, label, pad, _ in HOOKS:
        hb = _hook_bytes(label, pad, labels)
        buf[off:off + len(hb)] = hb

    problems = _validate(buf)
    if problems:
        raise TwoModeError("; ".join(problems))
    return _fix_sums(buf)


def set_params(data: bytes, params: dict[str, float]) -> bytearray:
    if not installed(data):
        raise TwoModeError("двухрежимка в этом файле не стоит")
    buf = bytearray(data)
    for name, value in params.items():
        _cal_put(buf, name, value)
    problems = _validate(buf)
    if problems:
        raise TwoModeError("; ".join(problems))
    return _fix_sums(buf)


def remove(data: bytes) -> bytearray:
    """Вернуть штатный код. Калибровки и KFZW2 остаются, но их никто не читает."""
    if not installed(data):
        raise TwoModeError("двухрежимка в этом файле не стоит")
    buf = bytearray(data)
    for off, orig, _, _, _ in HOOKS:
        buf[off:off + len(orig)] = orig
    buf[CODE_OFF:CODE_END] = b"\xff" * (CODE_END - CODE_OFF)
    buf[CAL_OFF:CAL_END] = b"\xff" * (CAL_END - CAL_OFF)
    return _fix_sums(buf)


def _fix_sums(buf: bytearray) -> bytearray:
    fixed, _ = bosch_csum.fix(fwlib.Firmware(bytes(buf)), bosch_csum.TABLE_ADDR)
    bad, _, _ = bosch_csum.check(fwlib.Firmware(bytes(fixed)), bosch_csum.TABLE_ADDR,
                                 verbose=False)
    if bad:
        raise TwoModeError("контрольные суммы не сошлись")
    return fixed


def show(data: bytes) -> None:
    if not installed(data):
        print("Двухрежимки нет.", "Штатный код на месте, можно ставить."
              if _pristine_ok(data) else "")
        return
    ver = struct.unpack_from("<H", data, CAL_OFF + 4)[0]
    print("Двухрежимка стоит, версия %d. Калибровки 0x%05X:" % (ver, CAL_OFF))
    for name, rel, fmt, k, unit, what in CAL:
        print("  0x%05X  %-8s %10g %-7s %s" % (CAL_OFF + rel, name, _cal_get(data, name), unit, what))
    print("  0x%05X  KFZW2 -- зажигание спорта, 12x16, оси как у KFZW%s"
          % (KFZW2, ", сейчас равна KFZW" if data[KFZW:KFZW + KFZW_SIZE]
             == data[KFZW2:KFZW2 + KFZW_SIZE] else ""))
    for i, rel in enumerate(KLLAMFA_SP):
        c = data[CAL_OFF + rel:CAL_OFF + rel + CURVE_LEN]
        n = c[0]
        axis = " ".join("%d" % (x * 40) for x in c[1:1 + n])
        lam = " ".join("%.3f" % (x * 0.0078125) for x in c[1 + n:1 + 2 * n])
        print("  0x%05X  KLLAMFA_SP[%d] об/мин: %s" % (CAL_OFF + rel, i, axis))
        print("           %s лямбда: %s" % (" " * len("KLLAMFA_SP[0]"), lam))


def _pristine_ok(data: bytes) -> bool:
    try:
        _check_pristine(data)
        return True
    except TwoModeError:
        return False


PARAM_ARGS = [
    ("--nmax-sport", "NMAX_SP"), ("--nlaunch", "NLAUNCH"), ("--vlaunch", "VLAU"),
    ("--wdk-launch", "WDKLAU"), ("--toggle-wdk", "WDKTOG"), ("--release-wdk", "WDKREL"),
    ("--toggle-time", "TTOGGLE"), ("--nstop", "NSTOP"),
]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Двухрежимка FBH3ID60: обычный и спорт")
    ap.add_argument("action", choices=["show", "install", "set", "remove", "listing"])
    ap.add_argument("firmware", nargs="?")
    ap.add_argument("-o", "--out")
    ap.add_argument("--keep-kfzw2", action="store_true",
                    help="не копировать KFZW в KFZW2 (спорт-зажигание уже подготовлено)")
    for flag, name in PARAM_ARGS:
        c = CAL_BY_NAME[name]
        ap.add_argument(flag, type=float, dest=name, help="%s, %s" % (c[5], c[4]))
    args = ap.parse_args(argv)

    if args.action == "listing":
        _, labels, listing = build()
        for addr, text in listing:
            names = [k for k, v in labels.items() if v == addr]
            for n in names:
                print("%s:" % n)
            print("  %06X  %s" % (addr, text))
        return 0
    if not args.firmware:
        ap.error("нужен файл прошивки")
    data = open(args.firmware, "rb").read()
    if args.action == "show":
        show(data)
        return 0
    if not args.out:
        ap.error("укажите -o <файл>")
    if args.out == args.firmware:
        print("Отказ: выходной файл совпадает с входным. Всегда сохраняйте оригинал.",
              file=sys.stderr)
        return 2
    params = {name: getattr(args, name) for _, name in PARAM_ARGS
              if getattr(args, name) is not None}
    try:
        if args.action == "install":
            out = install(data, params, copy_kfzw=not args.keep_kfzw2)
        elif args.action == "set":
            out = set_params(data, params)
        else:
            out = remove(data)
    except TwoModeError as e:
        print("Отказ: %s" % e, file=sys.stderr)
        return 1
    with open(args.out, "wb") as fh:
        fh.write(bytes(out))
    print("Записано: %s (контрольные суммы пересчитаны)" % args.out)
    show(bytes(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
