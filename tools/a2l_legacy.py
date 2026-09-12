#!/usr/bin/env python3
"""
a2l_legacy -- сборка A2L для СТАРЫХ версий WinOLS.

Отличия от обычного экспорта (tools/report.py):

  * ASAP2_VERSION 1 51 вместо 1 61 -- старые разборщики новее не понимают;
  * только ASCII: кириллица транслитерируется, потому что старый WinOLS
    спотыкается на не-ASCII в строках;
  * переводы строк CRLF;
  * имена не длиннее 32 символов, только [A-Za-z0-9_];
  * для карт зажигания подставлены НАСТОЯЩИЕ оси (AXIS_PTS + COM_AXIS),
    найденные по коду, вместо индексных;
  * FIX_AXIS применяется только там, где ось неизвестна.

Использование:
    python3 tools/a2l_legacy.py --maps out/FBH3ID60_maps.json \
        --profile profiles/FBH3ID60.json --out out/FBH3ID60_legacy.a2l
"""

from __future__ import annotations

import argparse
import json
import re

TYPE_U = {1: "UBYTE", 2: "UWORD"}
TYPE_S = {1: "SBYTE", 2: "SWORD"}

# Оси, установленные разбором кода (см. docs/07-код.md).
# addr -- адрес БАЙТА ДЛИНЫ; значения идут следом.
AXES = {
    "SNM16_ZU": dict(addr=0x10103, n=16, width=1, factor=40.0, offset=0.0,
                     unit="rpm", desc="engine speed, 16 points"),
    "SNM16_OP": dict(addr=0x100F2, n=16, width=1, factor=40.0, offset=0.0,
                     unit="rpm", desc="engine speed for KFZWOP, 16 points"),
    "SRL12_ZU": dict(addr=0x10157, n=12, width=1, factor=0.75, offset=0.0,
                     unit="%", desc="relative load, 12 points"),
    "SRL11_OP": dict(addr=0x14B70, n=11, width=2, factor=0.0234375, offset=0.0,
                     unit="%", desc="relative load for KFZWOP, 11 points"),
    "SNM16_GK": dict(addr=0x181F7, n=16, width=1, factor=40.0, offset=0.0,
                     unit="rpm", desc="engine speed for KFLBTS, 16 points"),
    "SRL12_GK": dict(addr=0x1821E, n=12, width=1, factor=0.75, offset=0.0,
                     unit="%", desc="relative load for KFLBTS, 12 points"),
}

# Какая карта какими осями пользуется: (ось строк = X, ось столбцов = Y)
MAP_AXES = {
    "KFZW":   ("SNM16_ZU", "SRL12_ZU"),
    "KFZW2":  ("SNM16_ZU", "SRL12_ZU"),
    "KFZWMS": ("SNM16_ZU", "SRL12_ZU"),
    "KFZWOP": ("SNM16_OP", "SRL11_OP"),
    "KFLBTS": ("SNM16_GK", "SRL12_GK"),
}

RU = {
    'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'e','ж':'zh','з':'z',
    'и':'i','й':'y','к':'k','л':'l','м':'m','н':'n','о':'o','п':'p','р':'r',
    'с':'s','т':'t','у':'u','ф':'f','х':'h','ц':'c','ч':'ch','ш':'sh','щ':'sch',
    'ъ':'','ы':'y','ь':'','э':'e','ю':'yu','я':'ya',
}


def translit(s: str) -> str:
    """Кириллица -> латиница, всё остальное -> ASCII."""
    out = []
    for ch in s or "":
        low = ch.lower()
        if low in RU:
            t = RU[low]
            out.append(t.upper() if ch.isupper() and t else t)
        elif ord(ch) < 128:
            out.append(ch)
        else:
            out.append(" ")
    return "".join(out)


def ident(s: str, used: set) -> str:
    """Допустимый идентификатор ASAP2: <=32 символа, [A-Za-z0-9_], уникальный."""
    s = translit(s)
    s = re.sub(r"[^A-Za-z0-9_]", "_", s).strip("_") or "MAP"
    if s[0].isdigit():
        s = "M_" + s
    s = s[:32]
    base, i = s, 1
    while s in used:
        suf = "_%d" % i
        s = base[:32 - len(suf)] + suf
        i += 1
    used.add(s)
    return s


def comment(s: str) -> str:
    """Строка комментария A2L: ASCII, без кавычек, не длиннее разумного."""
    s = translit(s).replace('"', "'").replace("\\", "/")
    s = re.sub(r"\s+", " ", s).strip()
    return s[:200]


def cm_name(factor: float, offset: float, unit: str) -> str:
    if (not factor or abs(factor - 1.0) < 1e-12) and not offset:
        return "CM_IDENTITY"
    key = ("%.10g_%.10g" % (factor, offset)).replace("-", "n")
    key = key.replace(".", "p").replace("+", "").replace("e", "E")
    u = re.sub(r"[^A-Za-z0-9]", "", translit(unit or ""))
    return ("CM_" + key + ("_" + u if u else ""))[:32]


def cm_block(factor: float, offset: float, unit: str) -> str:
    """
    RAT_FUNC переводит физическое в сырое: raw = (phys + offset) / factor.
    В наших терминах phys = raw*factor - shift, значит COEFFS 0 1 shift 0 0 factor.
    """
    name = cm_name(factor, offset, unit)
    if name == "CM_IDENTITY":
        return ""
    dec = 3 if abs(factor) < 1 else 1
    u = comment(unit or "")
    return ('\n    /begin COMPU_METHOD %s\n      "scale %.6g"\n'
            '      RAT_FUNC "%%.%df" "%s"\n      COEFFS 0 1 %.10g 0 0 %.10g\n'
            '    /end COMPU_METHOD\n' % (name, factor, dec, u, offset, factor))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="A2L для старых версий WinOLS")
    ap.add_argument("--maps", required=True)
    ap.add_argument("--profile")
    ap.add_argument("--out", required=True)
    ap.add_argument("--project", default="FBH3ID60")
    ap.add_argument("--min-confidence", default="all",
                    choices=["all", "verified"],
                    help="verified -- только подтверждённые карты")
    args = ap.parse_args(argv)

    maps = json.load(open(args.maps, encoding="utf-8"))["maps"]
    profile = json.load(open(args.profile, encoding="utf-8")) if args.profile else {}
    if args.min_confidence == "verified":
        maps = [m for m in maps if m.get("confidence") == "подтверждена"]
    maps = sorted(maps, key=lambda m: m["addr"])

    used: set = set()
    layouts: dict[str, str] = {}
    compus: dict[str, str] = {}
    axis_objs: list[str] = []
    chars: list[str] = []

    # --- объекты осей -------------------------------------------------
    for an, a in AXES.items():
        rl = "RL_AXIS_U%d" % (a["width"] * 8)
        layouts.setdefault(rl,
            '\n    /begin RECORD_LAYOUT %s\r\n      NO_AXIS_PTS_X 1 UBYTE\r\n'
            '      AXIS_PTS_X    2 %s INDEX_INCR DIRECT\r\n    /end RECORD_LAYOUT\r\n'
            % (rl, TYPE_U[a["width"]]))
        cm = cm_name(a["factor"], a["offset"], a["unit"])
        b = cm_block(a["factor"], a["offset"], a["unit"])
        if b:
            compus.setdefault(cm, b)
        used.add(an)
        axis_objs.append(
            '\n    /begin AXIS_PTS %s\n      "%s"\n      0x%X\n'
            '      NO_INPUT_QUANTITY\n      %s\n      0\n      %s\n'
            '      %d\n      %.6g\n      %.6g\n    /end AXIS_PTS\n'
            % (an, comment(a["desc"]), a["addr"], rl, cm, a["n"],
               a["offset"], a["n"] * 0 + a["factor"] * ((1 << (8 * a["width"])) - 1)))

    # --- карты ---------------------------------------------------------
    for m in maps:
        nm = ident(m.get("name") or ("MAP_%05X" % m["addr"]), used)
        is3d = m["kind"] == "3d" and m["ny"] > 1
        dw, aw = m["data_width"], m.get("axis_width", 1)
        signed = bool(m.get("signed"))
        dtype = (TYPE_S if signed else TYPE_U)[dw]
        bare = m["layout"] == "bare_grid"

        if bare:
            rl = "RL_GRID_%s" % dtype
            layouts.setdefault(rl,
                '\n    /begin RECORD_LAYOUT %s\r\n      FNC_VALUES 1 %s ROW_DIR DIRECT\r\n'
                '    /end RECORD_LAYOUT\r\n' % (rl, dtype))
        else:
            rl = "RL_%s_A%d_%s" % ("3D" if is3d else "2D", aw * 8, dtype)
            if is3d:
                layouts.setdefault(rl,
                    '\n    /begin RECORD_LAYOUT %s\r\n      NO_AXIS_PTS_X 1 UBYTE\r\n'
                    '      NO_AXIS_PTS_Y 2 UBYTE\r\n'
                    '      AXIS_PTS_X    3 %s INDEX_INCR DIRECT\r\n'
                    '      AXIS_PTS_Y    4 %s INDEX_INCR DIRECT\r\n'
                    '      FNC_VALUES    5 %s ROW_DIR DIRECT\r\n    /end RECORD_LAYOUT\r\n'
                    % (rl, TYPE_U[aw], TYPE_U[aw], dtype))
            else:
                layouts.setdefault(rl,
                    '\n    /begin RECORD_LAYOUT %s\r\n      NO_AXIS_PTS_X 1 UBYTE\r\n'
                    '      AXIS_PTS_X    2 %s INDEX_INCR DIRECT\r\n'
                    '      FNC_VALUES    3 %s ROW_DIR DIRECT\r\n    /end RECORD_LAYOUT\r\n'
                    % (rl, TYPE_U[aw], dtype))

        fac = m.get("factor") or 1.0
        off = m.get("shift") or 0.0
        cm = cm_name(fac, off, m.get("unit", ""))
        b = cm_block(fac, off, m.get("unit", ""))
        if b:
            compus.setdefault(cm, b)

        lo = (-(1 << (8 * dw - 1))) if signed else 0
        hi = ((1 << (8 * dw - 1)) - 1) if signed else ((1 << (8 * dw)) - 1)
        plo, phi = lo * fac - off, hi * fac - off
        if plo > phi:
            plo, phi = phi, plo

        # --- описания осей ---------------------------------------------
        pair = MAP_AXES.get(m.get("name") or "")
        descr = ""
        for k, npts in ((0, m["nx"]), (1, m["ny"])):
            if k == 1 and not is3d:
                break
            # у нас ширина = ось Y (столбцы), высота = ось X (строки)
            if pair:
                ref = pair[1] if k == 0 else pair[0]
                a = AXES[ref]
                acm = cm_name(a["factor"], a["offset"], a["unit"])
                amax = a["factor"] * ((1 << (8 * a["width"])) - 1)
                descr += ('\n      /begin AXIS_DESCR COM_AXIS\n'
                          '        NO_INPUT_QUANTITY\n        %s\n        %d\n'
                          '        0\n        %.6g\n        AXIS_PTS_REF %s\n'
                          '      /end AXIS_DESCR' % (acm, a["n"], amax, ref))
            elif bare:
                descr += ('\n      /begin AXIS_DESCR FIX_AXIS\n'
                          '        NO_INPUT_QUANTITY\n        CM_IDENTITY\n        %d\n'
                          '        0\n        %d\n        FIX_AXIS_PAR_DIST 0 1 %d\n'
                          '      /end AXIS_DESCR' % (npts, npts - 1, npts))
            else:
                amax = (1 << (8 * aw)) - 1
                descr += ('\n      /begin AXIS_DESCR STD_AXIS\n'
                          '        NO_INPUT_QUANTITY\n        CM_IDENTITY\n        %d\n'
                          '        0\n        %d\n      /end AXIS_DESCR' % (npts, amax))

        cmt = "%s %s addr 0x%X" % (m.get("confidence", ""), m.get("source", ""), m["addr"])
        if m.get("note"):
            cmt += " | " + m["note"]
        chars.append(
            '\n    /begin CHARACTERISTIC %s\n      "%s"\n      %s\n      0x%X\n'
            '      %s\n      0\n      %s\n      %.6g\n      %.6g%s\n'
            '    /end CHARACTERISTIC\n'
            % (nm, comment(cmt), "MAP" if is3d else "CURVE", m["addr"], rl, cm,
               plo, phi, descr))


    # --- всё подтверждённое из профиля ---------------------------------
    # Профиль накопил находки, которых нет в maps.json: контур детонации,
    # адсорбер, плёночная модель, моментная модель, блок катализатора.
    # Берём из него всё, у чего есть адрес и имя, кроме уже выданного.
    done_addr = {m["addr"] for m in maps}
    extra = []

    def collect(node):
        if isinstance(node, dict):
            a, nm = node.get("addr"), node.get("name")
            if isinstance(a, str) and a.startswith("0x") and nm:
                addr = int(a, 16)
                if addr not in done_addr:
                    done_addr.add(addr)
                    rows = node.get("rows") or node.get("height") or 1
                    cols = node.get("cols") or node.get("width") or 1
                    n = node.get("n") or 1
                    cnt = max(1, rows * cols, n)
                    extra.append(dict(name=nm, addr=addr, count=cnt,
                                      width=node.get("width_bytes") or node.get("width", 1) or 1,
                                      factor=node.get("factor") or 1.0,
                                      offset=node.get("offset") or 0.0,
                                      unit=node.get("unit", ""),
                                      desc=node.get("desc") or node.get("note", ""),
                                      conf=node.get("confidence", "")))
            for v in node.values():
                collect(v)
        elif isinstance(node, list):
            for v in node:
                collect(v)

    if args.min_confidence == "verified":
        keep = ("подтверждена",)
        collect(profile)
        extra = [e for e in extra if e["conf"] in keep]
    else:
        collect(profile)
    for e in extra:
        # у карт ширина ячейки лежит в width, а не в числе столбцов
        w = e["width"] if e["width"] in (1, 2) else 1
        cnt = e["count"] if e["count"] > 1 else 1
        dtype = TYPE_U[w]
        rl = "RL_GRID_%s" % dtype
        layouts.setdefault(rl,
            '\n    /begin RECORD_LAYOUT %s\r\n      FNC_VALUES 1 %s ROW_DIR DIRECT\r\n'
            '    /end RECORD_LAYOUT\r\n' % (rl, dtype))
        cm = cm_name(e["factor"], e["offset"], e["unit"])
        b = cm_block(e["factor"], e["offset"], e["unit"])
        if b:
            compus.setdefault(cm, b)
        hi = e["factor"] * ((1 << (8 * w)) - 1) - e["offset"]
        lo = -e["offset"]
        if lo > hi:
            lo, hi = hi, lo
        nm = ident(e["name"], used)
        cmt = (e["conf"] + " | " if e["conf"] else "") + e["desc"]
        if cnt > 1:
            descr = ('\n      /begin AXIS_DESCR FIX_AXIS\n        NO_INPUT_QUANTITY\n'
                     '        CM_IDENTITY\n        %d\n        0\n        %d\n'
                     '        FIX_AXIS_PAR_DIST 0 1 %d\n      /end AXIS_DESCR' % (cnt, cnt - 1, cnt))
            kind = "CURVE"
        else:
            descr, kind = "", "VALUE"
        chars.append('\n    /begin CHARACTERISTIC %s\n      "%s"\n      %s\n      0x%X\n'
                     '      %s\n      0\n      %s\n      %.6g\n      %.6g%s\n'
                     '    /end CHARACTERISTIC\n'
                     % (nm, comment(cmt), kind, e["addr"], rl, cm, lo, hi, descr))

    head = ('ASAP2_VERSION 1 51\n\n/begin PROJECT %s "Bosch M7.9.7 C167"\n\n'
            '  /begin HEADER "Kia Spectra 1.6 %s"\n    VERSION "1.0"\n  /end HEADER\n\n'
            '  /begin MODULE %s "Bosch M7.9.7"\n\n'
            '    /begin MOD_COMMON "little endian, alignment 1"\n'
            '      BYTE_ORDER MSB_LAST\n      ALIGNMENT_BYTE 1\n'
            '      ALIGNMENT_WORD 1\n      ALIGNMENT_LONG 1\n    /end MOD_COMMON\n\n'
            '    /begin COMPU_METHOD CM_IDENTITY\n      "raw values"\n'
            '      RAT_FUNC "%%.3f" ""\n      COEFFS 0 1 0 0 0 1\n'
            '    /end COMPU_METHOD\n'
            % (args.project, args.project, args.project + "_MOD"))
    tail = "\n  /end MODULE\n\n/end PROJECT\n"

    body = head
    for b in compus.values():
        body += b
    for b in layouts.values():
        body += b
    for b in axis_objs:
        body += b
    for c in chars:
        body += c
    body += tail

    body = body.replace("\r\n", "\n").replace("\n", "\r\n")
    data = body.encode("ascii", errors="replace")
    open(args.out, "wb").write(data)

    print("Записано: %s" % args.out)
    print("  карт: %d, осей: %d, раскладок: %d, пересчётов: %d"
          % (len(chars), len(axis_objs), len(layouts), len(compus) + 1))
    print("  ASAP2 1.51, только ASCII, переводы строк CRLF, размер %d байт" % len(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
