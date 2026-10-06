#!/usr/bin/env python3
"""
launch -- лаунч-контроль (2-step) для FBH3ID60 / Bosch M7.9.7.

В стоке лаунча нет. Здесь он дописывается кодом: на месте, где прошивка
решает «отключить подачу на всех цилиндрах», к стоковому условию
(перегрев, 0xFD6E.5) добавляется своё -- машина стоит, газ выше порога,
мотор прогрет, а обороты выше заданных. Разбор кода, почему врезка именно
сюда, а не в NMAX, и чем это проверено: docs/40-лаунч-контроль.md.

Что меняется в образе (код одинаков во всех FBH3ID60 из firmware/):

    0x3C116  6 байт   jb 0xfd6e.5 / jmpr  ->  calls 0x86FC00 / jmpr cc_Z
    0x6FC00  78 байт  подпрограмма (было пусто, 0xFF)
    0x17000  8 байт   параметры лаунча (было пусто, 0xFF; страница DPP1)

Параметры (страница DPP1 = файл 0x14000, как у NMAX):

    0x17000  LCNMAX    u16  x0.25 об/мин   выше -- подача отключается
    0x17002  LCDNH     u16  x0.25 об/мин   гистерезис: подача вернётся ниже
                                           LCNMAX - LCDNH
    0x17004  LCVMAX    u8   x1.25 км/ч     скорость (0x881D) не выше
    0x17005  LCWDKMN   u8   x0.390625 %    дроссель (0x89F3) не ниже
    0x17006  LCTMOTMN  u8   x0.75 - 48 °C  ОЖ (0x8AB4) не ниже
    0x17007  --        u8   резерв, 0xFF

    python3 tools/launch.py show    FW.bin
    python3 tools/launch.py apply   FW.bin -o FW_lc.bin --nmax 4200
    python3 tools/launch.py set     FW_lc.bin -o FW_lc2.bin --nmax 4500
    python3 tools/launch.py remove  FW_lc.bin -o FW_back.bin
    python3 tools/launch.py listing FW_lc.bin

apply/set/remove сами пересчитывают контрольные суммы (bosch_csum) и
проверяют результат; --no-csum оставляет их как есть.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bosch_csum  # noqa: E402
import fwlib       # noqa: E402

SIZE = 0x80000
BASE = 0x800000              # файл 0 -> адрес C167 0x800000

# место врезки: решение «отключить подачу на всех цилиндрах» (0xFD16.5)
HOOK = 0x3C116
HOOK_ORIG = bytes.fromhex("8A370150" "0D02")   # jb 0xfd6e.5, +1 / jmpr cc_UC, +2

# подпрограмма -- в пустом хвосте сегмента 0x86
CODE = 0x6FC00
CODE_ADDR = BASE + CODE      # 0x86FC00
CODE_LEN = 0x4E

# параметры -- в пустом хвосте калибровок страницы DPP1 (0x14000..0x17FFF)
PARAMS = 0x17000
PARAMS_LEN = 8
DPP1_FILE = 0x14000          # DPP1 = 0x205 -> файл 0x14000 (NMAX 0x4BB4 = 0x14BB4)


def dpp1(off: int) -> int:
    """Смещение в файле -> 16-битный адрес через DPP1 (0x4000..0x7FFF)."""
    assert DPP1_FILE <= off < DPP1_FILE + 0x4000
    return 0x4000 + (off - DPP1_FILE)


# Отпечаток функции решения об отключении подачи 0x83C0A4..0x83C224 без
# шести байт врезки: код один во всех FBH3ID60, и патч рассчитан на него.
FUNC = (0x3C0A4, 0x3C224)
FUNC_SHA256 = "174b380fb36891bec2636d4757996b72e7a52f05eb90b6ef02fb737e13824446"

NMAX_ADDR = 0x14BB4          # стоковая отсечка, только для подсказок

# --------------------------------------------------------------------------
# параметры

FIELDS = [
    # имя      смещение ширина множ.     сдвиг  ед.       по умолчанию
    ("LCNMAX",   0, 2, 0.25,      0.0, "об/мин", 4200.0),
    ("LCDNH",    2, 2, 0.25,      0.0, "об/мин", 200.0),
    ("LCVMAX",   4, 1, 1.25,      0.0, "км/ч",   2.5),
    ("LCWDKMN",  5, 1, 0.390625,  0.0, "%",      70.0),
    ("LCTMOTMN", 6, 1, 0.75,    -48.0, "°C",     60.0),
]
DESC = {
    "LCNMAX":   "выше этих оборотов подача отключается",
    "LCDNH":    "гистерезис: подача вернётся ниже LCNMAX - LCDNH",
    "LCVMAX":   "лаунч только при скорости (0x881D) не выше",
    "LCWDKMN":  "и при дросселе (0x89F3) не ниже",
    "LCTMOTMN": "и при ОЖ (0x8AB4) не ниже",
}


def to_raw(name: str, value: float) -> int:
    for n, _o, w, k, b, _u, _d in FIELDS:
        if n == name:
            raw = round((value - b) / k)
            if not 0 <= raw < (1 << (8 * w)):
                raise ValueError("%s = %s вне диапазона ячейки" % (name, value))
            return raw
    raise KeyError(name)


def to_phys(name: str, raw: int) -> float:
    for n, _o, _w, k, b, _u, _d in FIELDS:
        if n == name:
            return raw * k + b
    raise KeyError(name)


def read_params(data: bytes) -> dict:
    out = {}
    for n, o, w, _k, _b, _u, _d in FIELDS:
        a = PARAMS + o
        out[n] = data[a] if w == 1 else data[a] | (data[a + 1] << 8)
    return out


def write_params(buf: bytearray, raw: dict) -> None:
    for n, o, w, _k, _b, _u, _d in FIELDS:
        a = PARAMS + o
        if w == 1:
            buf[a] = raw[n]
        else:
            buf[a] = raw[n] & 0xFF
            buf[a + 1] = raw[n] >> 8
    buf[PARAMS + 7] = 0xFF


def default_raw() -> dict:
    return {n: to_raw(n, d) for n, _o, _w, _k, _b, _u, d in FIELDS}


def check_params(raw: dict, nmax_raw: int | None = None) -> list[str]:
    """Отказы (ValueError) и предупреждения (возвращаются списком)."""
    warn = []
    if raw["LCNMAX"] < to_raw("LCNMAX", 1500):
        raise ValueError("LCNMAX ниже 1500 об/мин -- это уже не лаунч")
    if raw["LCDNH"] >= raw["LCNMAX"]:
        raise ValueError("LCDNH должен быть меньше LCNMAX")
    if raw["LCDNH"] < to_raw("LCDNH", 40):
        warn.append("LCDNH меньше 40 об/мин: решение принимается раз в 10 мс, "
                    "гистерезиса фактически не будет")
    if raw["LCDNH"] > to_raw("LCDNH", 400):
        warn.append("LCDNH больше 400 об/мин: длинные провалы, и при LCNMAX ниже "
                    "4000 дольше отсечка в окне проверки датчика скорости")
    if nmax_raw is not None and raw["LCNMAX"] >= nmax_raw:
        warn.append("LCNMAX (%.0f) не ниже NMAX (%.0f): штатный ограничитель "
                    "сработает раньше лаунча" % (to_phys("LCNMAX", raw["LCNMAX"]),
                                                  nmax_raw * 0.25))
    if raw["LCVMAX"] > to_raw("LCVMAX", 5):
        warn.append("LCVMAX больше 5 км/ч: отсечка будет работать на ходу")
    if raw["LCWDKMN"] < to_raw("LCWDKMN", 40):
        warn.append("LCWDKMN ниже 40 %: лаунч включится и при обычном трогании")
    return warn


# --------------------------------------------------------------------------
# подпрограмма

def assemble(code_addr: int = CODE_ADDR) -> bytes:
    """
    Собрать подпрограмму. Вход: вызов из 0x83C116. Выход: флаг Z.
    Z = 0 -- отключить подачу на всех цилиндрах, Z = 1 -- нет.
    Портит только r4 (рабочий регистр по соглашению компилятора; в точке
    врезки r4 перезаписывается раньше, чем читается).

    Все используемые кодировки встречаются в стоковом коде этой же
    прошивки, а результат сверяется дизассемблером из c166.sinc (тест).
    """
    p = {n: dpp1(PARAMS + o) for n, o, *_ in FIELDS}
    lo = lambda v: v & 0xFF            # noqa: E731
    hi = lambda v: (v >> 8) & 0xFF     # noqa: E731

    # (метка, байты или функция(адрес, метки) -> байты, длина)
    prog = []

    def emit(raw: bytes, label: str | None = None):
        prog.append((label, raw, len(raw)))

    def br(fn, n: int, label: str | None = None):
        prog.append((label, fn, n))

    def rel(at: int, n: int, target: int) -> int:
        d = (target - (at + n)) // 2
        assert (target - (at + n)) % 2 == 0 and -128 <= d <= 127
        return d & 0xFF

    def jb(bitoff: int, bit: int, label: str, neg: bool = False):
        op = 0x9A if neg else 0x8A
        br(lambda at, L: bytes([op, bitoff, rel(at, 4, L[label]), bit << 4]), 4)

    def jmpr(cc: int, label: str):
        br(lambda at, L: bytes([(cc << 4) | 0x0D, rel(at, 2, L[label])]), 2)

    CC_C, CC_UGT = 0x8, 0xE
    R4, RL4 = 0xF4, 0xF8

    # стоковое условие -- перегрев (NOVH/DNOVH) -- как было
    jb(0x37, 5, "CUT")                                  # jb   0xfd6e.5, CUT
    # неисправен датчик скорости или дросселя -- лаунча нет
    emit(bytes([0xF2, R4, 0x7C, 0xB3]))                 # mov  r4, 0xb37c
    jb(R4, 0, "NOCUT")                                  # jb   r4.0, NOCUT
    emit(bytes([0xF2, R4, 0x04, 0xB3]))                 # mov  r4, 0xb304
    jb(R4, 0, "NOCUT")                                  # jb   r4.0, NOCUT
    # скорость не выше LCVMAX
    emit(bytes([0xF3, RL4, 0x1D, 0x88]))                # movb RL4, 0x881d
    emit(bytes([0x43, RL4, lo(p["LCVMAX"]), hi(p["LCVMAX"])]))
    jmpr(CC_UGT, "NOCUT")
    # дроссель не ниже LCWDKMN
    emit(bytes([0xF3, RL4, 0xF3, 0x89]))                # movb RL4, 0x89f3
    emit(bytes([0x43, RL4, lo(p["LCWDKMN"]), hi(p["LCWDKMN"])]))
    jmpr(CC_C, "NOCUT")
    # ОЖ не ниже LCTMOTMN
    emit(bytes([0xF3, RL4, 0xB4, 0x8A]))                # movb RL4, 0x8ab4
    emit(bytes([0x43, RL4, lo(p["LCTMOTMN"]), hi(p["LCTMOTMN"])]))
    jmpr(CC_C, "NOCUT")
    # порог: LCNMAX, а пока подача отключена -- LCNMAX - LCDNH
    emit(bytes([0xF2, R4, lo(p["LCNMAX"]), hi(p["LCNMAX"])]))
    jb(0x0B, 5, "CMP", neg=True)                        # jnb  0xfd16.5, CMP
    emit(bytes([0x22, R4, lo(p["LCDNH"]), hi(p["LCDNH"])]))
    jmpr(CC_C, "NOCUT")                                 # заём -- порог < 0
    emit(bytes([0x42, R4, 0xA0, 0xF8]), "CMP")          # cmp  r4, 0xf8a0
    jmpr(CC_C, "CUT")                                   # порог < nmot
    emit(bytes([0xE0, 0x04]), "NOCUT")                  # mov  r4, #0 -> Z=1
    emit(bytes([0xDB, 0x00]))                           # rets
    emit(bytes([0xE0, 0x14]), "CUT")                    # mov  r4, #1 -> Z=0
    emit(bytes([0xDB, 0x00]))                           # rets

    labels, at = {}, code_addr
    for label, _raw, n in prog:
        if label:
            labels[label] = at
        at += n
    out, at = bytearray(), code_addr
    for _label, raw, n in prog:
        b = raw(at, labels) if callable(raw) else raw
        assert len(b) == n
        out += b
        at += n
    assert len(out) == CODE_LEN, len(out)
    return bytes(out)


def hook_bytes(code_addr: int = CODE_ADDR) -> bytes:
    seg, off = code_addr >> 16, code_addr & 0xFFFF
    # calls seg:off ; jmpr cc_Z, +2 (-> 0x83C120, bclr: подачу не трогать)
    return bytes([0xDA, seg, off & 0xFF, off >> 8, 0x2D, 0x02])


# --------------------------------------------------------------------------
# состояние образа

def func_sha(data: bytes) -> str:
    a, b = FUNC
    body = bytearray(data[a:b])
    body[HOOK - a:HOOK - a + len(HOOK_ORIG)] = HOOK_ORIG
    return hashlib.sha256(bytes(body)).hexdigest()


def state(data: bytes) -> str:
    """'stock' -- можно ставить, 'patched' -- стоит наш, иначе причина."""
    if len(data) != SIZE:
        return "размер не 512 КБ"
    if func_sha(data) != FUNC_SHA256:
        return "код вокруг места врезки не тот (не FBH3ID60?)"
    hook = bytes(data[HOOK:HOOK + 6])
    code = bytes(data[CODE:CODE + CODE_LEN])
    if hook == HOOK_ORIG:
        if set(data[CODE:CODE + CODE_LEN]) != {0xFF} or \
                set(data[PARAMS:PARAMS + PARAMS_LEN]) != {0xFF}:
            return "место под код или параметры занято"
        return "stock"
    if hook == hook_bytes() and code == assemble():
        return "patched"
    return "врезка на 0x3C116 чужая или повреждена"


def apply(data: bytes, raw: dict) -> bytearray:
    st = state(data)
    if st != "stock":
        raise ValueError("ставить нельзя: " + ("лаунч уже стоит" if st == "patched" else st))
    buf = bytearray(data)
    buf[CODE:CODE + CODE_LEN] = assemble()
    write_params(buf, raw)
    buf[HOOK:HOOK + 6] = hook_bytes()
    return buf


def set_params(data: bytes, raw: dict) -> bytearray:
    if state(data) != "patched":
        raise ValueError("лаунча в образе нет -- сначала apply")
    buf = bytearray(data)
    write_params(buf, raw)
    return buf


def remove(data: bytes) -> bytearray:
    if state(data) != "patched":
        raise ValueError("лаунча в образе нет")
    buf = bytearray(data)
    buf[HOOK:HOOK + 6] = HOOK_ORIG
    buf[CODE:CODE + CODE_LEN] = b"\xFF" * CODE_LEN
    buf[PARAMS:PARAMS + PARAMS_LEN] = b"\xFF" * PARAMS_LEN
    return buf


def fix_sums(buf: bytearray) -> tuple[bytearray, int, int]:
    fw = fwlib.Firmware(bytes(buf))
    out, fixed = bosch_csum.fix(fw, bosch_csum.TABLE_ADDR)
    bad, _sk, good = bosch_csum.check(fwlib.Firmware(bytes(out)),
                                      bosch_csum.TABLE_ADDR, verbose=False)
    if bad:
        raise RuntimeError("контрольные суммы не сошлись после пересчёта")
    return out, fixed, len(good)


# --------------------------------------------------------------------------
# печать

def show_params(data: bytes) -> None:
    raw = read_params(data)
    for n, o, _w, _k, _b, u, _d in FIELDS:
        print("  0x%05X %-8s %8.2f %-6s -- %s" % (PARAMS + o, n, to_phys(n, raw[n]),
                                               u, DESC[n]))
    on = to_phys("LCNMAX", raw["LCNMAX"])
    off = on - to_phys("LCDNH", raw["LCDNH"])
    vmax = to_phys("LCVMAX", raw["LCVMAX"])
    print("  итого: показание скорости не выше %.2f км/ч (на деле медленнее "
          "%.2f -- 0x881D округляет вниз),\n         газ от %.1f %%, ОЖ от "
          "%.2f °C -> подача отключается выше %.0f и возвращается ниже "
          "%.0f об/мин"
          % (vmax, vmax + 1.25, to_phys("LCWDKMN", raw["LCWDKMN"]),
             to_phys("LCTMOTMN", raw["LCTMOTMN"]), on, off))


def listing(data: bytes | None) -> None:
    import c166dis
    code = assemble() if data is None else bytes(data[CODE:CODE + CODE_LEN])
    dis = c166dis.Disassembler()
    names = {dpp1(PARAMS + o): n for n, o, *_ in FIELDS}
    for i in c166dis.disassemble(code, 0, len(code), base=CODE_ADDR, dis=dis):
        txt = i.text()
        for a, n in names.items():
            txt = txt.replace("0x%04x" % a, "%s(0x%04x)" % (n, a))
        print("  %06X  %-12s %s" % (i.addr, i.raw.hex(" ").upper(), txt))


def gather(a, base: dict) -> dict:
    raw = dict(base)
    for n, opt in (("LCNMAX", a.nmax), ("LCDNH", a.dn), ("LCVMAX", a.vmax),
                   ("LCWDKMN", a.wdk), ("LCTMOTMN", a.tmot)):
        if opt is not None:
            raw[n] = to_raw(n, opt)
    return raw


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Лаунч-контроль, Bosch M7.9.7 FBH3ID60")
    ap.add_argument("action", choices=["show", "apply", "set", "remove", "listing"])
    ap.add_argument("firmware", nargs="?")
    ap.add_argument("-o", "--out")
    ap.add_argument("--nmax", type=float, help="обороты отсечки, об/мин (4200)")
    ap.add_argument("--dn", type=float, help="гистерезис, об/мин (200)")
    ap.add_argument("--vmax", type=float, help="скорость не выше, км/ч (2.5)")
    ap.add_argument("--wdk", type=float, help="дроссель не ниже, %% (70)")
    ap.add_argument("--tmot", type=float, help="ОЖ не ниже, °C (60)")
    ap.add_argument("--no-csum", action="store_true",
                    help="не пересчитывать контрольные суммы")
    a = ap.parse_args(argv)

    if a.action == "listing" and not a.firmware:
        listing(None)
        return 0
    if not a.firmware:
        ap.error("нужен файл прошивки")
    data = open(a.firmware, "rb").read()
    st = state(data)

    if a.action == "show":
        print("%s: %s" % (a.firmware, {"stock": "лаунча нет, поставить можно",
                                        "patched": "лаунч стоит"}.get(st, st)))
        if st == "patched":
            show_params(data)
        return 0
    if a.action == "listing":
        if st != "patched":
            print("лаунча в образе нет; печатаю то, что было бы записано")
        listing(data if st == "patched" else None)
        return 0

    if not a.out:
        ap.error("нужен -o")
    if os.path.abspath(a.out) == os.path.abspath(a.firmware):
        raise SystemExit("отказ: выходной файл совпадает с входным, "
                         "исходник надо сохранить")

    if a.action in ("set", "remove") and st != "patched":
        raise SystemExit("отказ: лаунча в образе нет" +
                         (" -- сначала apply" if a.action == "set" else ""))
    if a.action == "apply" and st != "stock":
        raise SystemExit("отказ: ставить нельзя: " +
                         ("лаунч уже стоит" if st == "patched" else st))

    if a.action == "remove":
        buf = remove(data)
    else:
        base = default_raw() if a.action == "apply" else read_params(data)
        raw = gather(a, base)
        nmax = data[NMAX_ADDR] | (data[NMAX_ADDR + 1] << 8)
        for w in check_params(raw, nmax):
            print("  внимание: " + w)
        buf = apply(data, raw) if a.action == "apply" else set_params(data, raw)

    if not a.no_csum:
        buf, fixed, good = fix_sums(buf)
        print("контрольные суммы: пересчитано %d, проверка %d из 32" % (fixed, good))
    open(a.out, "wb").write(bytes(buf))
    print("записано %s" % a.out)
    if a.action != "remove":
        show_params(bytes(buf))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
    except (ValueError, RuntimeError) as e:
        sys.exit("отказ: %s" % e)
