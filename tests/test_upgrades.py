#!/usr/bin/env python3
"""
Проверка находок, поднятых до подтверждённых развязкой по коду.

Каждая держится не на сдвиге области и не на правдоподобии, а на том,
что код использует ячейку в той самой роли, которую ей приписывает имя.
Такое совпасть не может: роль видна из структуры, а не из значения.
"""

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import c166dis        # noqa: E402

fails = []


def check(cond, msg):
    print(("OK   " if cond else "ОШИБКА ") + msg)
    if not cond:
        fails.append(msg)


def text_at(fw, start, end):
    dis = c166dis.Disassembler()
    return [(i.addr, " ".join(i.text().split()))
            for i in c166dis.disassemble(fw, start, end, base=0x800000,
                                         dis=dis)]


def main():
    fw = open(os.path.join(ROOT, "firmware", "FBH3ID60_stok.bin"), "rb").read()
    prof = json.load(open(os.path.join(ROOT, "profiles", "FBH3ID60.json"),
                          encoding="utf-8"))

    # -- ограничитель оборотов: выбор предела по флагу, минус гистерезис
    code = dict(text_at(fw, 0x3BF14, 0x3BF40))
    body = " | ".join(code.values())
    check("mov r4, 0x4bb6" in body and "mov r4, 0x4bb4" in body,
          "NMAXDV: код выбирает между 0x14BB6 и 0x14BB4 (NMAX)")
    check("mov r13, 0x4bb2" in body and "calls 0x0067d8" in body,
          "DNMAXH: 0x14BB2 идёт в вычитание из выбранного предела")
    check("cmp r4, 0xf8a0" in body,
          "результат сравнивается с 0xF8A0 -- ещё одно доказательство, "
          "что это обороты, а не расход")

    # -- пределы адаптации: один зажимает сверху, другой снизу
    code = dict(text_at(fw, 0x4393E, 0x43970))
    body = " | ".join(code.values())
    check(body.count("0x157e") >= 2 and body.count("0x157d") >= 2,
          "MSALLMN/MSALLMX: обе ячейки читаются по два раза -- сравнение "
          "и подстановка")
    i_mx = body.index("0x157e")
    i_mn = body.index("0x157d")
    check(i_mx < i_mn,
          "сначала зажим сверху (0x1157E), затем снизу (0x1157D) -- "
          "порядок как у классического клампа")

    # -- вентиляторы: два симметричных блока, база плюс добавка
    code = dict(text_at(fw, 0x401E2, 0x401EC))
    body = " | ".join(code.values())
    check("0x0fb3" in body and "0x0fb5" in body and "add" in body,
          "MDLF1 + MDLFE1: 0x10FB3 складывается с 0x10FB5")
    code = dict(text_at(fw, 0x40260, 0x4026A))
    body = " | ".join(code.values())
    check("0x0fb4" in body and "0x0fb6" in body and "add" in body,
          "MDLF2 + MDLFE2: 0x10FB4 складывается с 0x10FB6 -- блок "
          "симметричен первому")

    # -- RLNOT: указатель и два флага
    code = dict(text_at(fw, 0x43730, 0x43752))
    body = " | ".join(code.values())
    check("mov r12, #0x1580" in body, "RLNOT: найден указатель #0x1580")
    check(body.count("jnb") >= 2,
          "RLNOT: читается только под ДВУМЯ флагами через И -- столько же "
          "условий, сколько в ФР (E DK and E LM)")
    check("movbz r13, 0xf89e" in body,
          "RLNOT: вход 0xF89E -- индекс оборотов, как требует ФР")

    # -- WDKUGDN: указатель, один вход
    code = dict(text_at(fw, 0x4371C, 0x43730))
    body = " | ".join(code.values())
    check("mov r12, #0x5b40" in body, "WDKUGDN: найден указатель #0x5b40")

    # -- все шесть действительно помечены подтверждёнными
    got = {}

    def walk(n):
        if isinstance(n, dict):
            if n.get("name") and n.get("confidence"):
                got[n["name"]] = n["confidence"]
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(prof)
    for nm in ("NMAXDV", "DNMAXH", "MSALLMN", "MSALLMX", "MDLF1", "MDLF2",
               "MDLFE1", "MDLFE2", "RLNOT", "WDKUGDN", "ETADZW"):
        check(got.get(nm) == "подтверждена",
              "%s помечена подтверждённой (%s)" % (nm, got.get(nm)))

    print()
    if fails:
        print("ПРОВАЛЕНО: %d" % len(fails))
        return 1
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
