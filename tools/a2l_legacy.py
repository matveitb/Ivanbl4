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
import os
import re
import sys

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


# Названия разделов профиля по-русски: ключи в JSON английские, а в дереве
# редактора и WinOLS человек должен видеть контур, а не имя поля.
SECTION_TITLES = {
    "cyclic_charge": "Цикловое наполнение",
    "torque_model": "Моментная модель",
    "knock_control": "Контур детонации",
    "fuel_path": "Топливоподача",
    "fuel_trim_curves": "Коррекции топливоподачи",
    "fuel_path_unchanged": "Топливоподача, неизменное",
    "transient_fuel": "Переходные режимы, плёнка",
    "full_load_lambda": "Лямбда полной нагрузки",
    "alpha_map_KFLF": "Карта альфа KFLF",
    "second_lambda": "Второй датчик кислорода",
    "lambda_cat_diag_block": "Диагностика катализатора",
    "canister_purge": "Адсорбер",
    "cpv_diagnostics_block": "Диагностика адсорбера",
    "overrun_fuel_cut": "Отсечка на принудительном холостом",
    "max_charge_at_wot": "Наполнение на полной нагрузке",
    "immobilizer": "Иммобилайзер",
    "flag_tables": "Таблицы флагов",
    "scalars": "Одиночные величины",
    "axis_blocks": "Оси",
    "axes_resolved_from_code": "Оси, восстановленные по коду",
    "code_facts": "Найденное по коду",
    "other_regions": "Прочие области",
    "checksum": "Контрольные суммы",
    "identification": "Идентификация",
    "flash": "Флеш",
    "hfm_path": "Воздушный тракт, ДМРВ",
    "throttle_air_model": "Дроссель, модель воздуха",
    "saint_venant": "Сен-Венан, расход через дроссель",
    "maps": "Карты из описи",
    "scanner": "Найдено сканером",
    "scanner:подтверждена": "Сканер: подтверждённые",
    "scanner:вероятная": "Сканер: вероятные",
    "scanner:сомнительная": "Сканер: сомнительные",
    "scanner:неоднозначная": "Сканер: неоднозначные",
}


# Вес доказательства. Перекрывающиеся карты не могут быть верны обе:
# байты одни, а описания разные. Слабейшая уходит.
WEIGHT = {
    "подтверждена": 3,
    "вероятная": 2,
    "неоднозначная": 1,
    "сомнительная": 0,
}
# Профиль -- это то, что подтверждено чтением кода в этом проекте, а опись
# собрана выравниванием чужого дамоса. При прочих равных профиль весомее.
WEIGHT_PROFILE = 5


try:
    from ru_names import ru_name
except Exception:                                           # noqa: BLE001
    def ru_name(name, note="", points=None):                # noqa: D103
        return ""

CYR = re.compile("[А-Яа-яЁё]")


def short_ru(text: str, limit: int = 58) -> str:
    """
    Короткое русское название из описания.

    В самом A2L описания лежат транслитом -- файл делается ASCII-only ради
    старых версий WinOLS, и кириллице там взяться неоткуда. А в профиле и
    описи текст русский, и первая фраза почти всегда и есть название:
    «Основная карта угла опережения зажигания. Столбцы = нагрузка...».
    Берём её, остальное отбрасываем.

    Возвращает пустую строку, если русского текста нет: подписывать карту
    транслитом хуже, чем не подписывать вовсе.
    """
    if not text or not CYR.search(text):
        return ""
    head = re.split(r"[.;]|\s--\s", text.strip(), maxsplit=1)[0].strip()
    head = re.sub(r"\s+", " ", head)
    # Двоеточие отрезаем, только если до него уже сказано достаточно.
    # «требуемое наполнение из требуемого момента: обороты x момент» --
    # название слева, разметка осей справа, режем. А «кодовое слово:
    # адаптация расхода включена» слева не значит ничего, оставляем целиком.
    if ":" in head:
        left = head.split(":", 1)[0].strip()
        if len(left) >= 25:
            head = left
    if not CYR.search(head):
        return ""
    head = head[0].upper() + head[1:]
    if len(head) > limit:
        cut = head[:limit].rsplit(" ", 1)[0]
        head = (cut or head[:limit]).rstrip(",: ") + "..."
    return head


def resolve_overlaps(chars, meta):
    """
    Выбросить из описания карты, которые лезут на чужие байты.

    Две карты с пересекающимися данными не могут быть верны обе. Поэтому
    сравниваем вес доказательства и выбрасываем слабейшую. При РАВНОМ весе
    не выбрасываем ничего: это настоящий спор, и решать его должен человек,
    а не порядок в списке.

    Проход повторяется, пока что-то выбрасывается: убрав большую слабую
    карту (FVRMDYN на пять байт, накрывшую KUMSRL), мы освобождаем тех,
    кого она накрывала.
    """
    alive = list(range(len(meta)))
    dropped, unresolved = [], []
    seen_pairs = set()

    def note_pair(i, j):
        pair = tuple(sorted((meta[i]["name"], meta[j]["name"])))
        if pair in seen_pairs:
            return
        seen_pairs.add(pair)
        unresolved.append((pair[0], pair[1],
                           "%s / %s" % (meta[i]["why"], meta[j]["why"])))

    while True:
        order = sorted((i for i in alive if meta[i]["size"] > 0),
                       key=lambda i: meta[i]["start"])
        kill = None
        for a in range(len(order)):
            if kill:
                break
            i = order[a]
            mi = meta[i]
            for b in range(a + 1, len(order)):
                j = order[b]
                mj = meta[j]
                if mj["start"] >= mi["start"] + mi["size"]:
                    break
                # Безымянная находка сканера, севшая на НАЗВАННУЮ карту,
                # -- это она же и есть, только без имени: MAP_117_113A3
                # лежит ровно на KFPUSUNW, MAP_184_14A11 на KFMIOP.
                # Решается раньше весов и раньше защиты подтверждённых:
                # спора тут нет, есть дубликат без имени.
                ni = meta[i]["name"].startswith(("MAP_", "GRID_"))
                nj = meta[j]["name"].startswith(("MAP_", "GRID_"))
                if ni != nj:
                    kill = ((j, i) if nj else (i, j))
                    break
                if mi["weight"] == mj["weight"]:
                    note_pair(i, j)
                    continue
                loser, winner = (j, i) if mj["weight"] < mi["weight"] else (i, j)
                # ПОДТВЕРЖДЁННУЮ КАРТУ НЕ ВЫБРАСЫВАЕМ НИКОГДА, чей бы вес
                # ни был выше. Если две подтверждённые лезут друг на друга,
                # это не мусор, а признак, что неверна одна из них -- и
                # скорее всего внешняя, большая. Такое надо читать глазами,
                # а не разрешать перевесом.
                if meta[loser]["protect"]:
                    note_pair(i, j)
                    continue
                kill = (loser, winner)
                break
        if not kill:
            break
        loser, winner = kill
        alive.remove(loser)
        dropped.append((meta[loser]["name"], meta[loser]["why"],
                        meta[winner]["name"], meta[winner]["why"]))

    keep = set(alive)
    return ([chars[i] for i in sorted(keep)],
            [meta[i] for i in sorted(keep)], dropped, unresolved)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="A2L для старых версий WinOLS")
    ap.add_argument("--maps", required=True)
    ap.add_argument("--profile")
    ap.add_argument("--out", required=True)
    ap.add_argument("--project", default="FBH3ID60")
    ap.add_argument("--min-confidence", default="all",
                    choices=["all", "verified"],
                    help="verified -- только подтверждённые карты")
    ap.add_argument("--firmware",
                    help="образ прошивки: по нему проверяются оси и "
                         "разрешаются перекрытия. Без него описание всё равно "
                         "выпускается, но непроверенным")
    args = ap.parse_args(argv)

    image = None
    if args.firmware:
        with open(args.firmware, "rb") as fh:
            image = fh.read()

    maps = json.load(open(args.maps, encoding="utf-8"))["maps"]
    profile = json.load(open(args.profile, encoding="utf-8")) if args.profile else {}
    if args.min_confidence == "verified":
        maps = [m for m in maps if m.get("confidence") == "подтверждена"]
    maps = sorted(maps, key=lambda m: m["addr"])

    # --- разделы профиля -> группы --------------------------------------
    # Группа в ASAP2 -- это стандартный блок GROUP, и дерево по нему
    # строит не только наш редактор, но и WinOLS. Раздел верхнего уровня
    # профиля берём как группу, а принадлежность запоминаем ПО АДРЕСУ:
    # тогда карта, пришедшая из maps.json (KFZW, KFMIRL), тоже попадёт в
    # свой контур, а не в общую кучу "найдено сканером".
    sec_of_addr: dict[int, str] = {}

    def _sections(node, section):
        if isinstance(node, dict):
            a = node.get("addr")
            if isinstance(a, str) and a.startswith("0x"):
                try:
                    sec_of_addr.setdefault(int(a, 16), section)
                except ValueError:
                    pass
            for v in node.values():
                _sections(v, section)
        elif isinstance(node, list):
            for v in node:
                _sections(v, section)

    for _sec, _node in profile.items():
        _sections(_node, _sec)

    # Где сканер и профиль расходятся в адресе под ОДНИМ именем, имя
    # достаётся профилю: там находка подтверждена по коду, а у сканера --
    # выравниванием чужого дамоса. Запись сканера при этом не выбрасывается,
    # а получает имя с адресом: спор видно, и разрешать его должен человек,
    # а не генератор молча.
    prof_named: dict[str, set] = {}

    def _prof_names(node):
        if isinstance(node, dict):
            a, nm = node.get("addr"), node.get("name")
            if isinstance(a, str) and a.startswith("0x") and nm \
                    and not nm.startswith("MAP_"):
                try:
                    prof_named.setdefault(nm, set()).add(int(a, 16))
                except ValueError:
                    pass
            for v in node.values():
                _prof_names(v)
        elif isinstance(node, list):
            for v in node:
                _prof_names(v)

    _prof_names(profile)
    disputed: list = []
    # Разбор спорных адресов лежит в самом профиле: решение принято чтением
    # кода один раз и записано, а не пересчитывается генератором заново.
    verdicts = {k: v for k, v in (profile.get("address_disputes") or {}).items()
                if isinstance(v, dict)}

    groups: dict[str, list[str]] = {}

    def in_group(section: str, name: str) -> None:
        groups.setdefault(section, []).append(name)

    used: set = set()
    layouts: dict[str, str] = {}
    compus: dict[str, str] = {}
    axis_objs: list[str] = []
    chars: list[str] = []
    meta: list[dict] = []          # то же, что chars, но пригодное для разбора
    ru_names: dict[str, str] = {}  # имя в A2L -> короткое русское название

    # --- объекты осей -------------------------------------------------
    for an, a in AXES.items():
        rl = "RL_AXIS_U%d" % (a["width"] * 8)
        # Ширина СЧЁТЧИКА совпадает с шириной самих точек, а не всегда байт.
        # У словных осей (SRL11OPUW на 0x14B70) счётчик тоже словный, и
        # данные начинаются на два байта дальше. С байтовым счётчиком
        # чтение съезжало на один байт и давало мусор -- поймано при
        # первом же открытии карты в редакторе.
        layouts.setdefault(rl,
            '\n    /begin RECORD_LAYOUT %s\r\n      NO_AXIS_PTS_X 1 %s\r\n'
            '      AXIS_PTS_X    2 %s INDEX_INCR DIRECT\r\n    /end RECORD_LAYOUT\r\n'
            % (rl, TYPE_U[a["width"]], TYPE_U[a["width"]]))
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
        raw_name = m.get("name") or ("MAP_%05X" % m["addr"])
        if raw_name in prof_named and m["addr"] not in prof_named[raw_name]:
            disputed.append((raw_name, m["addr"],
                             sorted(prof_named[raw_name])[0],
                             verdicts.get(raw_name, {}).get("evidence", "")))
            # без "@": ident() всё равно заменит его подчёркиванием, и
            # задуманное ИМЯ@АДРЕС выходило как ИМЯ_АДРЕС -- пишем сразу так
            raw_name = "%s_%05X" % (raw_name, m["addr"])
        nm = ident(raw_name, used)
        # Карта, которой профиль не знает, идёт в группу по достоверности:
        # шестьсот безымянных находок одной кучей -- это не дерево.
        in_group(sec_of_addr.get(m["addr"])
                 or ("scanner:" + (m.get("confidence") or "прочее")), nm)
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
                # Порядок в блоке: сперва ось СТРОК, потом ось столбцов.
                # Проверено по данным: у KFMDS, KFMIRL и четырёх из пяти
                # прямоугольных заголовочных карт подряд в файле лежат
                # значения ВТОРОЙ оси, то есть первая -- это строки.
                # Независимое подтверждение -- tools/torque.py, который
                # читает KFMDS именно так и даёт максимум момента ровно на
                # 4500 об/мин, как в паспорте. Писать X первым только
                # потому, что он называется X, значит объявить карту
                # транспонированной.
                layouts.setdefault(rl,
                    '\n    /begin RECORD_LAYOUT %s\r\n      NO_AXIS_PTS_Y 1 UBYTE\r\n'
                    '      NO_AXIS_PTS_X 2 UBYTE\r\n'
                    '      AXIS_PTS_Y    3 %s INDEX_INCR DIRECT\r\n'
                    '      AXIS_PTS_X    4 %s INDEX_INCR DIRECT\r\n'
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
        # у заголовочных карт первая в файле ось -- это строки (Y), значит
        # столбцов столько, сколько во второй; см. раскладку выше
        dims = ((0, m["ny"]), (1, m["nx"])) if (is3d and not bare and not pair) \
            else ((0, m["nx"]), (1, m["ny"]))
        for k, npts in dims:
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
        # Сперва выверенное имя, потом первая фраза русского описания.
        # Пословный перевод английских описаний выброшен: он давал
        # «P составляющая карта рециркуляция регулятор» -- выглядит
        # осмысленно и тем обманывает.
        ru = (ru_name(m.get("name") or "", m.get("note") or "", m.get("nx"))
              or short_ru(m.get("note") or ""))
        if ru:
            ru_names[nm] = ru
        meta.append(dict(name=nm, start=m.get("data_addr") or m["addr"],
                         size=m["nx"] * max(1, m["ny"]) * dw,
                         weight=WEIGHT.get(m.get("confidence"), 0),
                         protect=m.get("confidence") == "подтверждена",
                         why="опись, %s" % (m.get("confidence") or "без оценки")))


    # --- всё подтверждённое из профиля ---------------------------------
    # Профиль накопил находки, которых нет в maps.json: контур детонации,
    # адсорбер, плёночная модель, моментная модель, блок катализатора.
    # Берём из него всё, у чего есть адрес и имя, кроме уже выданного.
    #
    # Оговорка про имена. Автоматический сканер даёт безымянные MAP_x_y_n.
    # Если профиль знает настоящее имя Bosch по тому же адресу -- побеждает
    # профиль: MAP_122_11580_6 и RLNOT это одно и то же, и держать в A2L
    # первое вместо второго было бы потерей.
    named = set()

    def _named(node):
        if isinstance(node, dict):
            a, nm = node.get("addr"), node.get("name")
            if isinstance(a, str) and a.startswith("0x") and nm \
                    and not nm.startswith("MAP_"):
                try:
                    named.add(int(a, 16))
                except ValueError:
                    pass
            for v in node.values():
                _named(v)
        elif isinstance(node, list):
            for v in node:
                _named(v)

    _named(profile)
    before = len(maps)
    maps = [m for m in maps
            if not (m["addr"] in named and str(m.get("name", "")).startswith("MAP_"))]
    if before != len(maps):
        print("  безымянных карт заменено именами из профиля: %d"
              % (before - len(maps)), file=sys.stderr)
    # Запись профиля отбрасываем не только когда её адрес совпал с адресом
    # карты из описи, но и когда он совпал с адресом ЕЁ ДАННЫХ. У
    # заголовочных карт это разные числа: опись знает KFETAZW по заголовку
    # 0x101F4, профиль -- по данным 0x10216, и раньше в A2L выходили две
    # записи на одни и те же 256 байт. Та же история у RLVMXN и RLVSMXN,
    # где профиль указывает на счётчик, а опись на первое значение: это не
    # спор об адресе, а одна и та же карта с двух концов.
    done_addr = {m["addr"] for m in maps}
    done_addr |= {m["data_addr"] for m in maps if m.get("data_addr")}
    extra = []
    merged: list = []

    def points_at(addr, w, n):
        """Первое значение по адресу -- на случай, если это счётчик."""
        if image is None or addr + w > len(image):
            return None
        return int.from_bytes(image[addr:addr + w], "little")

    def same_map_from_other_end(addr, node):
        """
        Не описывает ли эта запись профиля ту же карту, что уже выпущена,
        только с другого конца -- от счётчика, а не от данных?

        Признак проверяемый, а не предполагаемый: по адресу записи лежит
        ЧИСЛО, равное её же количеству точек, а сразу за ним начинается
        уже известная карта. Так устроены RLVMXN (0x182B6 счётчик, 0x182B7
        данные) и RLVSMXN. Догадываться по расстоянию в байт нельзя:
        соседняя калибровка может случайно оказаться через байт.
        """
        n = node.get("n") or 0
        if not n:
            return 0
        for w in (1, 2):
            if addr + w in done_addr and points_at(addr, w, n) == n:
                return addr + w
        return 0

    def collect(node, section):
        if isinstance(node, dict):
            a, nm = node.get("addr"), node.get("name")
            if isinstance(a, str) and a.startswith("0x") and nm:
                addr = int(a, 16)
                twin = same_map_from_other_end(addr, node)
                if twin:
                    merged.append((nm, addr, twin))
                    done_addr.add(addr)
                elif addr not in done_addr:
                    done_addr.add(addr)
                    # ВНИМАНИЕ: "width" в этом профиле -- ШИРИНА ЯЧЕЙКИ В
                    # БАЙТАХ, а не число столбцов. Раньше здесь стояло
                    # cols = cols or width, и скаляр шириной в слово
                    # превращался в две ячейки: DNMAXH, NMAX, NMAXDV,
                    # NMXDKPU и TNMAXDV стоят через два байта, поэтому
                    # каждый залезал на следующий, и правка второй ячейки
                    # NMAX писала в NMAXDV.
                    rows = node.get("rows") or node.get("height") or 1
                    cols = node.get("cols") or 1
                    n = node.get("n") or 1
                    cnt = max(1, rows * cols, n)
                    extra.append(dict(name=nm, addr=addr, count=cnt,
                                      width=node.get("width_bytes") or node.get("width", 1) or 1,
                                      factor=node.get("factor") or 1.0,
                                      offset=node.get("offset") or 0.0,
                                      unit=node.get("unit", ""),
                                      desc=node.get("desc") or node.get("note", ""),
                                      conf=node.get("confidence", ""),
                                      signed=bool(node.get("signed")),
                                      rows=rows, cols=cols,
                                      ax=node.get("x_axis"),
                                      ay=node.get("y_axis"),
                                      section=section))
            for v in node.values():
                collect(v, section)
        elif isinstance(node, list):
            for v in node:
                collect(v, section)

    # Обходим профиль ПО РАЗДЕЛАМ, а не целиком: раздел верхнего уровня --
    # это и есть естественная группа ("моментная модель", "воздушный
    # тракт"), и запомнить её надо в тот момент, когда мы в неё спускаемся.
    for _sec, _node in profile.items():
        collect(_node, _sec)
    if args.min_confidence == "verified":
        extra = [e for e in extra if e["conf"] == "подтверждена"]
    axis_src: dict[str, tuple] = {}
    # адрес описанной вручную оси -> её имя, чтобы не плодить двойников
    by_addr = {a["addr"]: an for an, a in AXES.items()}
    bad_axes: list = []

    def _axis_values(addr, n, aw, factor, skip):
        """Прочитать точки оси из образа -- ровно так, как их прочтёт редактор."""
        if image is None:
            return None
        out = []
        for i in range(n):
            p = addr + skip + i * aw
            if p + aw > len(image):
                return None
            out.append(int.from_bytes(image[p:p + aw], "little") * factor)
        return out

    def axis_obj(src, tag, owner=""):
        """
        Ось профиля -> объект AXIS_PTS.

        Три вещи, на которых я уже споткнулся и которые теперь решаются, а
        не предполагаются.

        1. АДРЕС ОЗНАЧАЕТ РАЗНОЕ. У KFMIRL и KFMDS в профиле записан адрес
           САМИХ ТОЧЕК, а у KFMIOP -- начало блока со счётчиком. Приняв
           одно за другое, я выпустил ось оборотов, читающуюся как 113280,
           205480 вместо 440, 680.
        2. ШИРИНА ТОЧКИ НЕ ВСЕГДА СЛОВО. Ось оборотов на 0x100F2 --
           байтовая с множителем 40.
        3. ОСЬ МОЖЕТ БЫТЬ УЖЕ ОПИСАНА ВРУЧНУЮ. Обе оси KFMIOP -- это
           SNM16_OP и SRL11_OP из таблицы AXES, и в самом профиле стоит
           пометка "та же ось, что у KFZWOP".

        Поэтому: сперва ищем готовую ось по адресу, затем проверяем
        прочитанные точки по образу. Ось, которая не возрастает, НЕ
        ВЫПУСКАЕТСЯ вовсе -- честная ось-индекс лучше красивой и неверной.
        """
        n = int(src.get("n") or 0)
        if not n or not str(src.get("addr", "")).startswith("0x"):
            return None
        addr = int(src["addr"], 16)

        # 1. уже описанная вручную ось -- у неё и счётчик, и ширина верны
        for cand in (addr, addr - 1, addr - 2):
            an = by_addr.get(cand)
            if an and AXES[an]["n"] == n:
                a = AXES[an]
                axis_src.setdefault(an, (cm_name(a["factor"], a["offset"],
                                                 a["unit"]),
                                         a["factor"] * ((1 << (8 * a["width"])) - 1)))
                return an

        aw = int(src.get("width_bytes") or src.get("point_width") or 2)
        af = src.get("factor") or 1.0
        # 2. счётчик перед точками бывает, а бывает и нет -- решаем по образу.
        #    Без образа проверить нечем: выпускаем как есть и говорим об этом
        #    один раз в отчёте, а не делаем вид, что проверили.
        skip = 0
        if image is not None:
            ok = lambda v: v and all(v[i + 1] > v[i] for i in range(n - 1))
            vals = _axis_values(addr, n, aw, af, 0)
            if not ok(vals):
                alt = _axis_values(addr, n, aw, af, aw)
                if ok(alt):
                    skip, vals = aw, alt
            if not ok(vals):
                bad_axes.append((owner, tag, src.get("addr"), n, aw))
                return None

        key = "AX_%X_%s" % (addr + skip, tag)
        if key in used:
            return key
        arl = "RL_AXIS_BARE_%s" % TYPE_U[aw]
        layouts.setdefault(arl,
            '\n    /begin RECORD_LAYOUT %s\r\n'
            '      AXIS_PTS_X 1 %s INDEX_INCR DIRECT\r\n'
            '    /end RECORD_LAYOUT\r\n' % (arl, TYPE_U[aw]))
        acm = cm_name(af, 0.0, src.get("unit", ""))
        b = cm_block(af, 0.0, src.get("unit", ""))
        if b:
            compus.setdefault(acm, b)
        used.add(key)
        axis_src[key] = (acm, af * ((1 << (8 * aw)) - 1))
        axis_objs.append(
            '\n    /begin AXIS_PTS %s\n      "%s"\n      0x%X\n'
            '      NO_INPUT_QUANTITY\n      %s\n      0\n      %s\n'
            '      %d\n      0\n      %.6g\n    /end AXIS_PTS\n'
            % (key, comment(src.get("unit", "")), addr + skip, arl, acm, n,
               af * ((1 << (8 * aw)) - 1)))
        return key

    for e in extra:
        # у карт ширина ячейки лежит в width, а не в числе столбцов
        w = e["width"] if e["width"] in (1, 2) else 1
        cnt = e["count"] if e["count"] > 1 else 1
        dtype = (TYPE_S if e["signed"] else TYPE_U)[w]
        rl = "RL_GRID_%s" % dtype
        layouts.setdefault(rl,
            '\n    /begin RECORD_LAYOUT %s\r\n      FNC_VALUES 1 %s ROW_DIR DIRECT\r\n'
            '    /end RECORD_LAYOUT\r\n' % (rl, dtype))
        cm = cm_name(e["factor"], e["offset"], e["unit"])
        b = cm_block(e["factor"], e["offset"], e["unit"])
        if b:
            compus.setdefault(cm, b)
        span = (1 << (8 * w - 1)) if e["signed"] else ((1 << (8 * w)) - 1)
        hi = e["factor"] * span - e["offset"]
        lo = (-e["factor"] * span - e["offset"]) if e["signed"] \
            else -e["offset"]
        if lo > hi:
            lo, hi = hi, lo
        nm = ident(e["name"], used)
        in_group(sec_of_addr.get(e["addr"]) or e["section"], nm)
        cmt = (e["conf"] + " | " if e["conf"] else "") + e["desc"]
        # Двумерную карту профиля надо и объявлять двумерной, иначе она
        # разворачивается в ленту: KFMIRL показывалась строкой из 192
        # значений вместо 16 на 12, и ни таблицей, ни поверхностью
        # пользоваться было нельзя.
        #
        # Про то, какая ось где. В профиле у таких карт записаны x_axis и
        # y_axis по их адресам в блоке; первый лежит раньше. Подряд в
        # файле идут значения ВТОРОЙ оси -- значит она и есть столбцы (X
        # в ASAP2), а первая -- строки. Проверено по данным и сходится с
        # tools/torque.py, который читает KFMDS так же.
        ax_ref = axis_obj(e["ay"], "X", e["name"]) \
            if isinstance(e["ay"], dict) else None
        ay_ref = axis_obj(e["ax"], "Y", e["name"]) \
            if isinstance(e["ax"], dict) else None

        def com(ref, npts):
            acm, amax = axis_src[ref]
            return ('\n      /begin AXIS_DESCR COM_AXIS\n'
                    '        NO_INPUT_QUANTITY\n        %s\n        %d\n'
                    '        0\n        %.6g\n        AXIS_PTS_REF %s\n'
                    '      /end AXIS_DESCR' % (acm, npts, amax, ref))

        def fix(npts):
            return ('\n      /begin AXIS_DESCR FIX_AXIS\n        NO_INPUT_QUANTITY\n'
                    '        CM_IDENTITY\n        %d\n        0\n        %d\n'
                    '        FIX_AXIS_PAR_DIST 0 1 %d\n      /end AXIS_DESCR'
                    % (npts, npts - 1, npts))

        if ax_ref and ay_ref:
            nx_e = int(e["ay"].get("n") or 1)
            ny_e = int(e["ax"].get("n") or 1)
            descr = com(ax_ref, nx_e) + com(ay_ref, ny_e)
            kind = "MAP"
            cnt = nx_e * ny_e
        elif e["rows"] > 1 and e["cols"] > 1:
            # Осторожно с именами полей: в этом профиле "rows" на деле
            # означает ЧИСЛО СТОЛБЦОВ. Проверено на трёх картах, у которых
            # оси известны и ширину можно посчитать независимо: KFMIRL
            # (rows 12 -- это 12 столбцов на 16 строк), KFMDS (12 на 10),
            # KFMSNWDK (6 на 16). Брать поле по названию значило бы
            # объявить все такие карты перевёрнутыми.
            descr = fix(e["rows"]) + fix(e["cols"])
            kind = "MAP"
            cnt = e["rows"] * e["cols"]
        elif cnt > 1:
            descr = fix(cnt)
            kind = "CURVE"
        else:
            descr, kind = "", "VALUE"
        chars.append('\n    /begin CHARACTERISTIC %s\n      "%s"\n      %s\n      0x%X\n'
                     '      %s\n      0\n      %s\n      %.6g\n      %.6g%s\n'
                     '    /end CHARACTERISTIC\n'
                     % (nm, comment(cmt), kind, e["addr"], rl, cm, lo, hi, descr))
        ru = (ru_name(e["name"], e["desc"] or "", e["rows"])
              or short_ru(e["desc"] or ""))
        if ru:
            ru_names[nm] = ru
        meta.append(dict(name=nm, start=e["addr"], size=cnt * w,
                         weight=WEIGHT_PROFILE
                         if e["conf"] == "подтверждена" else WEIGHT_PROFILE - 1,
                         protect=e["conf"] == "подтверждена",
                         why="профиль, %s" % (e["conf"] or "без оценки")))

    # --- разрешение перекрытий ------------------------------------------
    chars, meta, dropped, unresolved = resolve_overlaps(chars, meta)
    kept = {d["name"] for d in meta}
    for sec in list(groups):
        groups[sec] = [n for n in groups[sec] if n in kept]

    # --- группы ---------------------------------------------------------
    group_objs: list[str] = []
    sub_names: list[str] = []
    for sec in sorted(groups, key=lambda k: (-len(groups[k]), k)):
        names = groups[sec]
        if not names:
            continue
        gname = ident("G_" + translit(sec), used)
        sub_names.append(gname)
        refs = "\n".join("        " + n for n in names)
        group_objs.append(
            '\n    /begin GROUP %s\n      "%s"\n'
            '      /begin REF_CHARACTERISTIC\n%s\n      /end REF_CHARACTERISTIC\n'
            '    /end GROUP\n'
            % (gname, comment(SECTION_TITLES.get(sec, sec)), refs))
    if group_objs:
        subs = "\n".join("        " + n for n in sub_names)
        group_objs.insert(0,
            '\n    /begin GROUP G_ALL\n      "Все карты"\n      ROOT\n'
            '      /begin SUB_GROUP\n%s\n      /end SUB_GROUP\n'
            '    /end GROUP\n' % subs)

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
    for g in group_objs:
        body += g
    body += tail

    body = body.replace("\r\n", "\n").replace("\n", "\r\n")
    data = body.encode("ascii", errors="replace")
    open(args.out, "wb").write(data)

    # Русские названия -- отдельным файлом рядом с A2L.
    #
    # В сам A2L их не положить: он ASCII-only ради старых версий WinOLS, и
    # кириллица там превратится в транслит. А подпись «Osnovnaya karta ugla
    # operezheniya zazhiganiya» читать невозможно. Поэтому имена живут
    # рядом в UTF-8: наш редактор их подхватывает, чужие разборщики просто
    # не замечают лишний файл.
    ru_out = os.path.splitext(args.out)[0] + ".ru.json"
    ru_names = {k: v for k, v in ru_names.items() if k in kept}
    with open(ru_out, "w", encoding="utf-8") as fh:
        json.dump(ru_names, fh, ensure_ascii=False, indent=1, sort_keys=True)

    print("Записано: %s" % args.out)
    print("  русские названия: %s (%d карт)"
          % (os.path.basename(ru_out), len(ru_names)))
    if image is None:
        print("  ОСИ И ПЕРЕКРЫТИЯ НЕ ПРОВЕРЕНЫ: образ не передан (--firmware)",
              file=sys.stderr)
    if merged:
        print("  сведено записей «счётчик против данных»: %d" % len(merged),
              file=sys.stderr)
        for nm_, a_, b_ in sorted(merged):
            print("    %-12s профиль 0x%05X -> уже описана с 0x%05X"
                  % (nm_, a_, b_), file=sys.stderr)
    if bad_axes:
        print("  ОСИ, НЕ ПРОШЕДШИЕ ПРОВЕРКУ (выпущены как индекс): %d"
              % len(bad_axes), file=sys.stderr)
        for owner, tag, a_, n_, w_ in bad_axes:
            print("    %-12s ось %s по %s: %d точек по %d байт не возрастают"
                  % (owner, tag, a_, n_, w_), file=sys.stderr)
    if dropped:
        print("  ВЫБРОШЕНО ПО ПЕРЕКРЫТИЮ: %d" % len(dropped), file=sys.stderr)
        for loser, lw, winner, ww in sorted(dropped):
            print("    %-22s (%s)  лез на  %-22s (%s)"
                  % (loser, lw, winner, ww), file=sys.stderr)
    if unresolved:
        print("  ПЕРЕКРЫТИЯ С РАВНЫМ ВЕСОМ -- решать человеку: %d"
              % len(unresolved), file=sys.stderr)
        for a_, b_, why in sorted(unresolved):
            print("    %-22s и %-22s (%s)" % (a_, b_, why), file=sys.stderr)
    # Сведённые записи -- не спор: там опись и профиль говорят об одной
    # карте с разных концов. В списке спорных им делать нечего.
    merged_names = {m[0] for m in merged}
    disputed = [d for d in disputed if d[0] not in merged_names]
    if disputed:
        solved = sum(1 for d in disputed if d[3])
        print("  СПОРНЫЕ АДРЕСА: %d, из них разобрано по коду %d. Имя "
              "остаётся за профилем, запись описи переименована в ИМЯ_АДРЕС"
              % (len(disputed), solved), file=sys.stderr)
        for nm_, scan, prof_a, why in sorted(disputed):
            print("    %-12s опись 0x%05X, профиль 0x%05X%s"
                  % (nm_, scan, prof_a,
                     ("  <- " + why) if why else "  (по коду не разобрано)"),
                  file=sys.stderr)
    print("  карт: %d, осей: %d, раскладок: %d, пересчётов: %d, групп: %d"
          % (len(chars), len(axis_objs), len(layouts), len(compus) + 1,
             max(0, len(group_objs) - 1)))
    print("  ASAP2 1.51, только ASCII, переводы строк CRLF, размер %d байт" % len(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
