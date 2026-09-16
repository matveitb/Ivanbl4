#!/usr/bin/env python3
"""
confirm -- поднять карты до подтверждённых ССЫЛКОЙ ИЗ КОДА.

Половина описи держится на выравнивании чужого дамоса: имя и адрес взяты
оттого, что в другой прошивке на похожем месте лежала похожая карта. Это
догадка. Ссылка из кода -- факт: если процессор грузит указатель на этот
адрес, значит там действительно начало таблицы.

Почему одним проходом. ptrfind разбирает весь код заново на каждый адрес,
и на семистах картах это часы. Здесь код разбирается ОДИН раз, из каждой
инструкции достаются все 16-битные числа, и уже по ним ищутся все карты
разом.

Планка намеренно высокая, и вот почему. Совпадение 16-битного числа само
по себе ничего не значит: 0x1485 встретится и как константа, и как кусок
другого адреса. Поэтому засчитывается только ЗАГРУЗКА НЕПОСРЕДСТВЕННОГО
ЗНАЧЕНИЯ (`mov r12, #0x1485`) -- так выглядит указатель на таблицу, --
и отдельно отмечается прямое чтение ячейки (`cmpb RL4, 0x1672`), которое
годится для скаляров, но не для карт.

Понижать достоверность нельзя ни при каких условиях. Отсутствие ссылки
НЕ значит, что карта неверна: к таблице часто обращаются через базу с
переменным смещением, и указателя на её начало в коде нет вовсе.

    python3 tools/confirm.py firmware/FBH3ID60_stok.bin \
        --maps results/FBH3ID60_maps.json --profile profiles/FBH3ID60.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import c166dis                                              # noqa: E402


def strict_forms(addr: int) -> list[int]:
    """
    Только ТЕ формы записи адреса, которые процессор действительно
    использует, и ни одной запасной.

    ptrfind добавляет к ним ещё `addr & 0x3FFF` и то же с 0x4000 -- на
    случай, если страница окажется иной. Для ручного поиска по одному
    адресу это разумная подстраховка: глазами видно, годится находка или
    нет. Для пакетного прогона по семистам картам -- нет: запасная форма
    ловит случайные константы, и в выборке мне сразу попались две карты,
    «подтверждённые» числом, которое к их адресу отношения не имеет.

    Соответствия проверены на KFLF, KFMSNWDK, KFPU, KFRLW (см. ptrfind).
    """
    if 0x10000 <= addr < 0x18000:
        return [addr - 0x10000]
    if 0x18000 <= addr < 0x20000:
        return [addr - 0x18000]
    return []

# `mov r12, #0x1485` -- загрузка указателя. Именно решётка отличает
# «адрес таблицы» от «прочитать ячейку по адресу».
IMM = re.compile(r"#0x([0-9a-f]{1,4})\b")
MEM = re.compile(r"(?<![#x0-9a-f])0x([0-9a-f]{1,4})\b")

# Инструкции работы с битовыми полями несут МАСКУ, а не адрес. `bfldl
# 0xfd00, #0x21, #0x2108` -- это установка битов регистра, и то, что
# 0x2108 совпало с формой чьего-то адреса, чистая случайность.
NOT_POINTER = re.compile(r"^(bfld[lh]|bset|bclr|bmov|band|bor|bxor|bcmp)\b")


def scan_code(fw: bytes, lo: int, hi: int):
    """Все 16-битные числа из кода: отдельно указатели, отдельно чтения."""
    imm: dict[int, list] = {}
    mem: dict[int, list] = {}
    dis = c166dis.Disassembler()
    for ins in c166dis.disassemble(fw, lo, hi, base=0x800000, dis=dis):
        t = " ".join(ins.text().split())
        if NOT_POINTER.match(t):
            continue
        for m in IMM.finditer(t):
            imm.setdefault(int(m.group(1), 16), []).append((ins.addr, t))
        for m in MEM.finditer(t):
            mem.setdefault(int(m.group(1), 16), []).append((ins.addr, t))
    return imm, mem


def evidence(addrs, imm, mem):
    """
    Чем код подтверждает эти адреса.

    Возвращает (вид, инструкция) либо (None, ""). Указатель весомее
    чтения: чтение ячейки годится для одиночной величины, но для карты
    из двухсот значений ничего не доказывает.
    """
    for a in addrs:
        for f in strict_forms(a):
            hit = imm.get(f)
            if hit:
                return "указатель", "%s @0x%06X" % (hit[0][1], hit[0][0])
    for a in addrs:
        for f in strict_forms(a):
            hit = mem.get(f)
            if hit:
                return "чтение", "%s @0x%06X" % (hit[0][1], hit[0][0])
    return None, ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Подтверждение карт по коду")
    ap.add_argument("firmware")
    ap.add_argument("--maps", required=True)
    ap.add_argument("--profile")
    ap.add_argument("--range", nargs=2, default=("0x20000", "0x80000"))
    ap.add_argument("--out", help="куда записать обновлённый maps.json")
    ap.add_argument("--profile-out",
                    help="куда записать обновлённый профиль")
    a = ap.parse_args(argv)

    fw = open(a.firmware, "rb").read()
    doc = json.load(open(a.maps, encoding="utf-8"))
    maps = doc["maps"]

    print("разбираю код 0x%s..0x%s одним проходом..."
          % (a.range[0], a.range[1]), file=sys.stderr)
    imm, mem = scan_code(fw, int(a.range[0], 0), int(a.range[1], 0))
    print("  различных непосредственных значений: %d, чтений: %d"
          % (len(imm), len(mem)), file=sys.stderr)

    up = ptr = rd = 0
    for m in maps:
        if m.get("confidence") == "подтверждена":
            continue
        cand = [m["addr"]]
        if m.get("data_addr"):
            cand.append(m["data_addr"])
        # у кривой с ведущим счётчиком указатель часто ведёт на счётчик
        cand.append(m["addr"] - 1)
        kind, why = evidence(cand, imm, mem)
        if not kind:
            continue
        # Для КАРТЫ засчитываем только указатель. Чтение одной ячейки не
        # говорит ничего о таблице из сотен значений -- на этом я уже
        # обжигался с WDKUGDN, где cmpb по адресу был, а карта там другая.
        cells = m.get("nx", 1) * max(1, m.get("ny", 1))
        if kind == "чтение" and cells > 2:
            continue
        # ВАЖНО: код подтверждает АДРЕС, а не ИМЯ.
        #
        # Указатель доказывает, что по этому адресу действительно начало
        # таблицы, к которой обращается прошивка. Он не говорит НИЧЕГО о
        # том, верно ли имя, пришедшее из выравнивания чужого дамоса.
        # Сперва я ставил «подтверждена» по одной только ссылке -- и у
        # 132 карт из 194 получилось, что адрес доказан, а имя взято из
        # слабого выравнивания. Владелец это и поймал: KFAGRP помечена
        # подтверждённой, хотя имя у неё из damos-сомнительно, а на его
        # моторе клапана EGR нет вовсе.
        #
        # Поэтому «подтверждена» только когда сходятся ОБА: адрес по коду
        # и имя из надёжного источника. Иначе -- отдельная оценка.
        m["addr_confirmed"] = "код: " + why
        src = m.get("source", "")
        strong = src in ("damos", "damos-точно", "профиль")
        m["confidence"] = "подтверждена" if strong else "адрес подтверждён"
        up += 1
        ptr += kind == "указатель"
        rd += kind == "чтение"

    # Профиль обходим тем же способом. Сперва я его пропустил, и KFMSNWDK
    # выпала из описания как «вероятная», хотя её заголовок грузится
    # указателем `mov r12, #0x5664` -- ровно то доказательство, по
    # которому подняты остальные двести.
    pup = 0
    if a.profile:
        prof = json.load(open(a.profile, encoding="utf-8"))

        def walk(node):
            nonlocal pup
            if isinstance(node, dict):
                ad, nm = node.get("addr"), node.get("name")
                if (isinstance(ad, str) and ad.startswith("0x") and nm
                        and node.get("confidence") != "подтверждена"):
                    base = int(ad, 16)
                    cand = [base, base - 1]
                    if isinstance(node.get("header"), str):
                        cand.append(int(node["header"], 16))
                    rows = node.get("rows") or 1
                    cols = node.get("cols") or 1
                    n = max(1, rows * cols, node.get("n") or 1)
                    kind, why = evidence(cand, imm, mem)
                    if kind == "указатель" or (kind and n <= 2):
                        # То же различие, что и для описи: профиль -- наша
                        # собственная работа по коду, поэтому имя в нём
                        # считается надёжным источником.
                        node["addr_confirmed"] = "код: " + why
                        node["confidence"] = "подтверждена"
                        pup += 1
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(prof)
        print("в профиле поднято: %d" % pup)
        if a.profile_out:
            with open(a.profile_out, "w", encoding="utf-8") as fh:
                json.dump(prof, fh, ensure_ascii=False, indent=1)
            print("записано: %s" % a.profile_out)

    print("поднято до подтверждённых: %d (по указателю %d, по чтению %d)"
          % (up, ptr, rd))
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=1)
        print("записано: %s" % a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
