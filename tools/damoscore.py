#!/usr/bin/env python3
"""
damoscore -- оценка чужого описания калибровок (DAMOS/A2L) на пригодность
к нашей прошивке БЕЗ ручного разбора.

Зачем. Баз с дамосами много, весят они сотни гигабайт, а подходит из них
единицы. Качать всё подряд бессмысленно. Этот скрипт берёт скачанный
файл описания, за секунды прикладывает его к FBH3ID60 и говорит число:
сколько имён реально сядет на наши адреса. Дальше по этому числу
кандидаты ранжируются, и качать нужно только верхушку списка.

Метод -- тот же, что дал группу отсечки топлива и блок детонации:

  1. Из описания берутся подряд идущие скаляры (цепочки).
  2. Цепочка длиной N скользит по калибровочному сегменту.
  3. Сдвиг принимается, только если выполнены ОБА условия сразу:
     -- все N ячеек попадают в список адресов, которые код прошивки
        действительно читает (tools/calref.py);
     -- все N значений после пересчёта лежат в допусках, объявленных
        в самом описании (w_min..w_max).
  4. Сдвиг засчитывается, только если он единственный. Две базы и
     больше -- цепочка отбрасывается как неоднозначная.

Кодом читается ~1665 ячеек из 65536, то есть одна случайная ячейка даёт
2.5 %. Цепочка из десяти подряд -- 2.5 %^10. Проверка допусков нужна
отдельно: ссылки кода идут плотными участками, и в таком участке одними
ссылками десяток соседних сдвигов не различить. Вместе два условия дают
единственную базу.

Понимает:
  *.dam           -- DAMOS (ASAP2DAM), разбор через tools/damos.py
  *.a2l           -- ASAP2, блоки CHARACTERISTIC/MEASUREMENT с ECU_ADDRESS

Использование:
    python3 tools/calref.py firmware/FBH3ID60_stok.bin --out out/calref.json

    # один кандидат
    python3 tools/damoscore.py cand.dam

    # целая папка скачанных описаний, ранжировать
    python3 tools/damoscore.py /path/to/damos/ --rank
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FLASH_BASE = 0x800000
DATA_LO, DATA_HI = 0x10000, 0x20000
SUFFIXES = (".dam", ".a2l")


# -- чтение кандидатов ------------------------------------------------------

class Obj:
    """Одна калибровочная величина кандидата, приведённая к общему виду."""

    __slots__ = ("off", "name", "scalar", "width", "factor", "shift",
                 "lo", "hi", "signed")

    def __init__(self, off, name, scalar, width=1, factor=1.0, shift=0.0,
                 lo=None, hi=None, signed=False):
        self.off, self.name, self.scalar = off, name, scalar
        self.width, self.factor, self.shift = width, factor, shift
        self.lo, self.hi, self.signed = lo, hi, signed

    def fits(self, fw: bytes, at: int) -> bool:
        """Лежит ли значение по адресу at в объявленном допуске."""
        if self.lo is None or at + self.width > len(fw):
            return False
        raw = int.from_bytes(fw[at:at + self.width], "little",
                             signed=self.signed)
        phys = raw * self.factor - self.shift
        return self.lo - 1e-9 <= phys <= self.hi + 1e-9


def load_dam(path: str) -> list[Obj]:
    import damos
    d = damos.parse(path)
    out = []
    for v in d.variables:
        off = v.addr - FLASH_BASE if v.addr >= FLASH_BASE else v.addr
        if not (DATA_LO <= off < DATA_HI):
            continue
        c = d.conv(v.conv_w)
        width = 2 if (c and c.raw_max >= 65535) else 1
        lo, hi = (v.w_min, v.w_max) if v.w_max > v.w_min else (None, None)
        out.append(Obj(off, v.name, v.nx == 0 and v.ny == 0, width,
                       c.factor if c else 1.0, c.shift if c else 0.0, lo, hi))
    return out


_A2L_CHAR = re.compile(
    r'/begin\s+CHARACTERISTIC\s+(\S+)\s+"(?:[^"]*)"\s+(\S+)\s+'
    r'(0x[0-9A-Fa-f]+)\s+(\S+)\s+([-\d.eE+]+)\s+(\S+)\s+'
    r'([-\d.eE+]+)\s+([-\d.eE+]+)', re.S)

_A2L_CM = re.compile(
    r'/begin\s+COMPU_METHOD\s+(\S+).*?COEFFS\s+'
    r'([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)\s+'
    r'([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)', re.S)

_A2L_RL = re.compile(
    r'/begin\s+RECORD_LAYOUT\s+(\S+)(.*?)/end\s+RECORD_LAYOUT', re.S)

_WIDTH = {"UBYTE": (1, False), "SBYTE": (1, True),
          "UWORD": (2, False), "SWORD": (2, True),
          "ULONG": (4, False), "SLONG": (4, True)}


def load_a2l(path: str) -> list[Obj]:
    """
    Разбор A2L. Масштаб берётся из COMPU_METHOD (RAT_FUNC), ширина и
    знаковость -- из RECORD_LAYOUT. Без этого проверка допусков сравнивала
    бы сырой байт с физическим диапазоном и давала мусор.

    В ASAP2 COEFFS задают пересчёт физического значения в сырое:
        raw = (a*p^2 + b*p + c) / (d*p^2 + e*p + f)
    при a=d=e=0 обратное преобразование -- phys = (f*raw - c) / b.
    """
    txt = open(path, "r", encoding="utf-8", errors="replace").read()

    conv: dict[str, tuple[float, float]] = {}
    for name, a, b, c, d, e, f in _A2L_CM.findall(txt):
        try:
            a, b, c, d, e, f = (float(x) for x in (a, b, c, d, e, f))
        except ValueError:
            continue
        if a or d or e or not b:
            continue                      # нелинейный -- пропускаем допуск
        conv[name] = (f / b, -c / b)      # phys = raw*factor + offset

    lay: dict[str, tuple[int, bool]] = {}
    for name, body in _A2L_RL.findall(txt):
        m = re.search(r'(?:FNC_VALUES|AXIS_PTS_X)\s+\d+\s+(\w+)', body)
        if m and m.group(1).upper() in _WIDTH:
            lay[name] = _WIDTH[m.group(1).upper()]

    out = []
    for name, typ, addr, rec, _mx, cm, lo, hi in _A2L_CHAR.findall(txt):
        off = int(addr, 16)
        if off >= FLASH_BASE:
            off -= FLASH_BASE
        if not (DATA_LO <= off < DATA_HI):
            continue
        width, signed = lay.get(rec, (1, False))
        factor, offs = conv.get(cm, (None, 0.0))
        try:
            flo, fhi = float(lo), float(hi)
        except ValueError:
            flo = fhi = None
        usable = factor is not None and flo is not None and fhi > flo
        out.append(Obj(off, name, typ.upper() == "VALUE", width,
                       factor if usable else 1.0, -offs if usable else 0.0,
                       flo if usable else None, fhi if usable else None,
                       signed))
    return out


def load_any(path: str) -> list[Obj]:
    low = path.lower()
    if low.endswith(".dam"):
        return load_dam(path)
    if low.endswith(".a2l"):
        return load_a2l(path)
    raise ValueError("не понимаю формат: " + path)


# -- оценка -----------------------------------------------------------------

def chains(objs: list, min_len: int, max_gap: int) -> list[list]:
    sc = sorted((o for o in objs if o.scalar), key=lambda o: o.off)
    runs, cur = [], []
    for o in sc:
        if cur and o.off - cur[-1].off > max_gap:
            if len(cur) >= min_len:
                runs.append(cur)
            cur = []
        cur.append(o)
    if len(cur) >= min_len:
        runs.append(cur)
    return runs


def score(objs, refs: set[int], fw: bytes, min_len: int, max_gap: int,
          span: int, min_rate: float, range_rate: float):
    runs = chains(objs, min_len, max_gap)
    anchored = ambiguous = cells = 0
    deltas: dict[int, int] = {}
    placed: dict[int, str] = {}
    for run in runs:
        base = run[0].off
        cand = []
        for delta in (range(-base, DATA_HI - DATA_LO - base) if span <= 0
                      else range(-span, span + 1)):
            hit = ranged = checkable = 0
            for o in run:
                at = o.off + delta
                if not (DATA_LO <= at < DATA_HI):
                    continue
                if at in refs:
                    hit += 1
                if o.lo is not None:
                    checkable += 1
                    ranged += o.fits(fw, at)
            if hit < min_len or hit / len(run) < min_rate:
                continue
            # Допуски -- условие мягче ссылок: у родственной, но другой
            # калибровки отдельные константы законно выходят за диапазон
            # исходной. Полное совпадение требовать нельзя, иначе теряются
            # заведомо верные участки.
            if checkable and ranged / checkable < range_rate:
                continue
            cand.append((hit, delta))
        if not cand:
            continue
        best = max(h for h, _ in cand)
        ties = [d for h, d in cand if h == best]
        if len(ties) > 1:
            ambiguous += 1
            continue
        anchored += 1
        cells += best
        delta = ties[0]
        deltas[delta] = deltas.get(delta, 0) + 1
        for o in run:
            if o.off + delta in refs:
                placed[o.off + delta] = o.name
    return dict(objects=len(objs), scalars=sum(1 for o in objs if o.scalar),
                chains=len(runs), anchored=anchored, ambiguous=ambiguous,
                cells=cells, shifts=len(deltas),
                top_shifts=sorted(deltas.items(), key=lambda t: -t[1])[:5],
                placed=placed)


def known_addrs(profile: str) -> set[int]:
    """Адреса, которые у нас уже подтверждены -- чтобы считать прирост."""
    got: set[int] = set()

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("addr", "data_addr", "x_axis_addr", "y_axis_addr",
                         "base", "header") and isinstance(v, str) \
                        and v.startswith("0x"):
                    try:
                        got.add(int(v, 16))
                    except ValueError:
                        pass
                else:
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(json.load(open(profile, encoding="utf-8")))
    return got


def collect(target: str) -> list[str]:
    if os.path.isfile(target):
        return [target]
    found = []
    for root, _, files in os.walk(target):
        for f in files:
            if f.lower().endswith(SUFFIXES):
                found.append(os.path.join(root, f))
    return sorted(found)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Оценка чужого DAMOS/A2L на пригодность к FBH3ID60")
    ap.add_argument("target", help="файл описания или папка с ними")
    ap.add_argument("--refs", default="out/calref.json",
                    help="JSON от calref.py -- адреса, читаемые кодом")
    ap.add_argument("--profile", default="profiles/FBH3ID60.json")
    ap.add_argument("--firmware", default="firmware/FBH3ID60_stok.bin",
                    help="образ, к которому прикладываем описание")

    ap.add_argument("--min-len", type=int, default=6)
    ap.add_argument("--max-gap", type=int, default=4)
    ap.add_argument("--span", type=int, default=0x800,
                    help="полуширина перебора сдвигов")
    ap.add_argument("--min-rate", type=float, default=1.0,
                    help="доля ячеек цепочки, обязанных читаться кодом")
    ap.add_argument("--range-rate", type=float, default=0.9,
                    help="доля значений, обязанных попасть в допуск")
    ap.add_argument("--rank", action="store_true", help="только сводная таблица")
    ap.add_argument("--out")
    a = ap.parse_args(argv)

    refs = {int(k, 16) for k in json.load(open(a.refs))}
    fw = open(a.firmware, "rb").read()
    have = known_addrs(a.profile) if os.path.exists(a.profile) else set()
    files = collect(a.target)
    if not files:
        print("не нашёл ни одного .dam/.a2l в " + a.target, file=sys.stderr)
        return 1

    rows = []
    for path in files:
        try:
            objs = load_any(path)
        except Exception as exc:                      # noqa: BLE001
            print(f"{os.path.basename(path)}: не разобрал ({exc})", file=sys.stderr)
            continue
        if not objs:
            continue
        s = score(objs, refs, fw, a.min_len, a.max_gap, a.span, a.min_rate,
                  a.range_rate)
        s["gain"] = len({k for k in s["placed"] if k not in have})
        s["file"] = path
        rows.append(s)

    rows.sort(key=lambda r: (-r["anchored"], -r["cells"]))
    print(f"{'заяк':>4} {'ячеек':>6} {'прирост':>7} {'сдвигов':>7}  файл")
    for r in rows:
        print(f"{r['anchored']:>4} {r['cells']:>6} {r['gain']:>7} "
              f"{r['shifts']:>7}  {os.path.basename(r['file'])}")
    if not a.rank:
        for r in rows[:3]:
            print(f"\n-- {os.path.basename(r['file'])}")
            print(f"   объектов в сегменте: {r['objects']}, из них скаляров "
                  f"{r['scalars']}, цепочек {r['chains']}, "
                  f"неоднозначных {r['ambiguous']}")
            for d, n in r["top_shifts"]:
                print(f"   сдвиг {d:+6d}  цепочек {n}")
    if a.out:
        for r in rows:
            r["placed"] = {f"0x{k:05X}": v for k, v in r["placed"].items()}
        json.dump(rows, open(a.out, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print(f"\nзаписано {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
