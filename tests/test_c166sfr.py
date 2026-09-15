#!/usr/bin/env python3
"""
Проверка разметки регистров C166/C167 по самому образу.

Документация Infineon из этой среды недоступна, поэтому карта записана
по архитектуре. Чтобы она не была словом на веру, каждый признак
проверяется тем, что подделать нельзя.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import c166sfr        # noqa: E402

fails = []


def check(cond, msg):
    print(("OK   " if cond else "ОШИБКА ") + msg)
    if not cond:
        fails.append(msg)


def main():
    # короткая форма адресации -- та самая, из-за которой поиск байтами не работает
    check(c166sfr.short_reg(0xFFA0) == 0xD0,
          "ADCON 0xFFA0 в короткой форме = регистр 0xD0")
    check(c166sfr.short_reg(0xFE00) == 0x00, "DPP0 -> регистр 0x00")
    check(c166sfr.short_reg(0x9368) is None, "внешний адрес короткой формы не имеет")

    check(c166sfr.name(0xFE00) == "DPP0", "0xFE00 это DPP0")
    # карта теперь из даташита, а не по памяти
    check(len(c166sfr.BY_ADDR) >= 200,
          "регистров из даташита: %d" % len(c166sfr.BY_ADDR))
    check(c166sfr.name(0xFF1C) == "ZEROS",
          "ZEROS 0xFF1C -- найден по коду, подтверждён даташитом")
    check(c166sfr.name(0xF0A0) == "ADDAT2", "ESFR разобраны: 0xF0A0 это ADDAT2")
    check(len(c166sfr.DATASHEET_DISCREPANCIES) == 2,
          "расхождений в самом даташите выписано: %d (T7IC, T8IC)"
          % len(c166sfr.DATASHEET_DISCREPANCIES))
    check(c166sfr.name(0xFEA0) == "ADDAT", "0xFEA0 это ADDAT")
    check("внутреннее ОЗУ" in c166sfr.area(0xF7B2),
          "0xF7B2 лежит во внутреннем ОЗУ")

    fw_path = os.path.join(ROOT, "firmware", "FBH3ID60_stok.bin")
    if not os.path.exists(fw_path):
        print("нет образа -- проверка по прошивке пропущена")
    else:
        fw = open(fw_path, "rb").read()
        for tag, ok, why in c166sfr.verify(fw):
            check(ok, "%s: %s" % (tag, why))

    print()
    if fails:
        print("ПРОВАЛЕНО: %d" % len(fails))
        return 1
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
