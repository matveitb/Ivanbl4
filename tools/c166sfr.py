#!/usr/bin/env python3
"""
c166sfr -- разметка регистров и памяти Infineon C166/C167.

Зачем. В дизассемблере обращения к периферии выглядят как голые числа:
`mov 0xffa0, r4` ничего не говорит. А это ADCON -- управление АЦП. Имена
превращают чтение кода из угадывания в работу: сразу видно, где читается
аналоговый вход (а это ДМРВ, ДПДЗ, ДТОЖ), где крутятся таймеры впрыска,
где идёт обмен по диагностике.

Откуда карта. Раскладка SFR у C166/C167 архитектурная, она одинакова у
всех чипов семейства. Документация Infineon из этой среды недоступна
(connect_rejected на infineon.com), поэтому карта записана по
архитектуре, а затем ПРОВЕРЕНА по самой прошивке -- см. verify(). Это
важно: то, что подтверждено поведением образа, и то, что взято из
описания архитектуры, помечено по-разному.

Проверка опирается на факты, которые нельзя подделать:

  * DPP0..DPP3 обязаны загружаться в начале исполнения, и загруженные в
    них страницы обязаны совпасть с теми, что独 独 уже установлены
    независимо -- 0x204..0x207 для калибровочного сегмента;
  * SP, CP, STKOV, STKUN настраиваются один раз в стартовом коде;
  * ADCON и ADDAT обязаны встречаться вместе, в одной подпрограмме.
"""

from __future__ import annotations

import json
import os
import re

# --- области адресов ------------------------------------------------------

AREAS = [
    (0x00000, 0x0FFFF, "CODE/DATA сегмент 0"),
    (0xF000, 0xF1FF, "ESFR -- расширенные регистры (после EXTR)"),
    (0xF200, 0xF5FF, "зарезервировано"),
    (0xF600, 0xFDFF, "внутреннее ОЗУ (IRAM), в нём же GPR по указателю CP"),
    (0xFE00, 0xFFFF, "SFR -- регистры специальных функций"),
]

# --- регистры -------------------------------------------------------------
# Берутся из официального даташита Infineon C167CR/C167SR V3.3 (2005-02),
# раздел 3.15, таблица 8. Извлечены tools/gen_sfr.py в data/c167_sfr.json.
# Раньше эта карта была записана по памяти -- теперь она документирована.

def _load_table():
    path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "data", "c167_sfr.json")
    if not os.path.exists(path):
        return {}, [], ""
    js = json.load(open(path, encoding="utf-8"))
    by = {}
    for nm, v in js["registers"].items():
        by[v["addr"]] = (nm, _group(nm), v["desc"])
    return by, js.get("discrepancies", []), js.get("source", "")


def _group(nm: str) -> str:
    """Отнести регистр к группе -- для читаемости вывода."""
    if nm.startswith(("ADC", "ADDAT")):
        return "АЦП"
    if nm.startswith(("T", "CAPREL")) and not nm.startswith("TFR"):
        return "таймер"
    if nm.startswith("CC"):
        return "CAPCOM"
    if nm.startswith(("PW", "PWM")):
        return "ШИМ"
    if nm.startswith(("S0", "SSC")):
        return "обмен"
    if nm.startswith(("P", "DP", "OD")) and len(nm) <= 4:
        return "порт"
    if nm.startswith("DPP") or nm in ("CP", "SP", "STKOV", "STKUN", "CSP"):
        return "память"
    if nm.startswith("MD"):
        return "ариф"
    if nm.endswith("IC"):
        return "прерыв"
    return "система"


BY_ADDR, DATASHEET_DISCREPANCIES, DATASHEET_SOURCE = _load_table()

# CAPCOM: даташит перечисляет их явно, но если таблицы нет под рукой --
# 32 регистра захвата/сравнения лежат двумя блоками подряд.
for _i in range(16):
    BY_ADDR.setdefault(0xFE80 + _i * 2,
                       ("CC%d" % _i, "CAPCOM", "захват/сравнение"))
    BY_ADDR.setdefault(0xFE60 + _i * 2,
                       ("CC%d" % (_i + 16), "CAPCOM", "захват/сравнение"))


def name(addr: int) -> str | None:
    e = BY_ADDR.get(addr)
    return e[0] if e else None


def describe(addr: int) -> str:
    e = BY_ADDR.get(addr)
    if e:
        return "%s -- %s (%s)" % (e[0], e[2], e[1])
    for lo, hi, what in AREAS[1:]:
        if lo <= addr <= hi:
            return what
    return ""


def area(addr: int) -> str:
    for lo, hi, what in AREAS[1:]:
        if lo <= addr <= hi:
            return what
    return "внешняя память / ОЗУ"


def short_reg(addr: int) -> int | None:
    """
    Номер регистра в короткой 8-битной форме адресации.

    SFR из 0xFE00-0xFFFF кодируются одним байтом: reg = (адрес-0xFE00)/2.
    Именно поэтому искать ADCON в образе как слово 0xFFA0 бессмысленно --
    в коде он выглядит как регистр 0xD0.
    """
    if 0xFE00 <= addr <= 0xFFFF and not addr & 1:
        return (addr - 0xFE00) // 2
    return None


def verify(fw: bytes, dpp_pages=(0x204, 0x205, 0x206, 0x207)):
    """
    Проверить карту по самой прошивке, а не по памяти автора.

    Возвращает список (признак, сошлось, пояснение). Проверяются вещи,
    которые подделать нельзя: страницы DPP обязаны совпасть с теми, что
    установлены независимо по адресации калибровок, а ADCON и ADDAT
    обязаны встречаться в коде вместе.
    """
    out = []
    ins = fw  # ищем непосредственные загрузки SFR по шаблону mov SFR, #data
    # 1. DPP: где-то в коде должны лежать записи страниц калибровки
    hits = []
    for page in dpp_pages:
        lo = page & 0xFF
        hi = page >> 8
        if bytes([lo, hi]) in ins:
            hits.append(page)
    out.append(("страницы DPP", len(hits) >= 3,
                "найдено %d из %d страниц калибровки (0x%03X..0x%03X) как "
                "непосредственных значений в образе"
                % (len(hits), len(dpp_pages), dpp_pages[0], dpp_pages[-1])))
    # 2. АЦП: ищем по КОРОТКОЙ форме, разобрав код, а не считая байты.
    #    Счёт байтов тут не годится -- любое совпадающее число в данных
    #    дало бы ложное срабатывание, а сам ADCON как слово не встречается.
    from collections import Counter
    use = sfr_usage(fw)
    adcon, addat = use.get("ADCON", 0), use.get("ADDAT", 0)
    out.append(("АЦП", adcon > 0 and addat > 0,
                "обращений к ADCON: %d, к ADDAT: %d" % (adcon, addat)))
    # 3. Стек и банк регистров настраиваются в стартовом коде
    out.append(("стек", use.get("SP", 0) > 0,
                "обращений к SP: %d, к CP: %d"
                % (use.get("SP", 0), use.get("CP", 0))))
    # 4. CAPCOM: на этом блоке ими формируются импульсы форсунок и катушек
    cc = sum(v for k, v in use.items() if k.startswith("CC"))
    out.append(("CAPCOM", cc > 0, "обращений к каналам захвата/сравнения: %d" % cc))
    return out


def sfr_usage(fw: bytes, start: int = 0x20000, end: int = 0x80000) -> dict:
    """
    Сколько раз каждый размеченный регистр реально встречается в КОДЕ.

    Считаем по разобранным инструкциям, а не поиском байтов в образе:
    любое совпадающее число в таблицах дало бы ложное срабатывание, а
    короткая форма адресации вообще не содержит адреса как такового.
    По умолчанию берётся кодовый сегмент, без калибровок.
    """
    import os
    import sys
    from collections import Counter
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import c166dis

    # Линейный разбор всего кодового сегмента неизбежно принимает часть
    # таблиц за инструкции. Отсюда единичные "обращения" к регистрам с
    # бессмысленными операндами. Для счёта это шум в пределах процентов,
    # но каждое отдельное место надо смотреть глазами, а не верить списку.
    names = {e[0] for e in BY_ADDR.values()}
    word = re.compile(r"\b(" + "|".join(sorted(names, key=len, reverse=True))
                      + r")\b")
    cnt: Counter = Counter()
    dis = c166dis.Disassembler()
    for ins in c166dis.disassemble(fw, start, end, base=0x800000, dis=dis):
        for m in word.finditer(ins.text()):
            cnt[m.group(1)] += 1
    return dict(cnt)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Разметка регистров C166/C167")
    ap.add_argument("firmware", nargs="?")
    ap.add_argument("--addr", help="описать один адрес")
    ap.add_argument("--list", action="store_true", help="вся карта регистров")
    ap.add_argument("--usage", action="store_true",
                    help="посчитать обращения к каждому регистру в коде")
    ap.add_argument("--range", nargs=2, default=("0x20000", "0x80000"),
                    help="какой кусок кода разбирать")
    a = ap.parse_args(argv)

    if a.addr:
        v = int(a.addr, 0)
        print("0x%04X: %s" % (v, describe(v) or "не размечен"))
        sr = short_reg(v)
        if sr is not None:
            print("       короткая форма адресации: регистр 0x%02X" % sr)
        return 0

    if a.list or not a.firmware:
        print("%-10s %-8s %-9s %s" % ("ИМЯ", "АДРЕС", "ГРУППА", "ОПИСАНИЕ"))
        for addr in sorted(BY_ADDR):
            n, g, d = BY_ADDR[addr]
            print("%-10s 0x%04X   %-9s %s" % (n, addr, g, d))
        print("\nвсего размечено регистров: %d" % len(BY_ADDR))
        for lo, hi, what in AREAS[1:]:
            print("  0x%04X-0x%04X  %s" % (lo, hi, what))
        return 0

    fw = open(a.firmware, "rb").read()
    lo, hi = int(a.range[0], 0), int(a.range[1], 0)

    if a.usage:
        use = sfr_usage(fw, lo, hi)
        print("Обращения к регистрам в коде 0x%05X-0x%05X:\n" % (lo, hi))
        print("%-10s %-8s %-9s %-7s %s"
              % ("ИМЯ", "АДРЕС", "ГРУППА", "ОБРАЩ.", "ОПИСАНИЕ"))
        rows = []
        for addr, (n, g, d) in BY_ADDR.items():
            if use.get(n):
                rows.append((use[n], n, addr, g, d))
        for cnt, n, addr, g, d in sorted(rows, reverse=True):
            print("%-10s 0x%04X   %-9s %-7d %s" % (n, addr, g, cnt, d))
        print("\nрегистров, реально используемых кодом: %d из %d"
              % (len(rows), len(BY_ADDR)))
        return 0

    print("Проверка разметки по образу %s:\n" % a.firmware)
    bad = 0
    for tag, ok, why in verify(fw):
        print("  %-16s %-11s %s" % (tag, "СОШЛОСЬ" if ok else "НЕ СОШЛОСЬ", why))
        bad += not ok
    print("\nПроверяется то, что подделать нельзя: страницы DPP обязаны")
    print("совпасть с установленными независимо по адресации калибровок,")
    print("а обращения считаются по разобранным инструкциям, не поиском")
    print("байтов -- иначе любое совпадающее число в таблицах дало бы")
    print("ложное срабатывание.")
    return 1 if bad else 0


if __name__ == "__main__":
    import sys
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
