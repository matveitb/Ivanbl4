#!/usr/bin/env python3
"""
meaning -- проверить СМЫСЛ каждой карты по тому, как ею пользуется код.

Ссылка из кода доказывает, что по адресу настоящая таблица. Но имя к ней
пришло из выравнивания чужого дамоса, и проверить его ссылка не может:
так у меня получились "карта регулятора AGR" на байтах кондиционера и
постоянная форсунки внутри плёночной карты.

Имя проверяется величиной. У дамоса есть пересчёт: `nmot_ub_q40` -- это
обороты, `temp_ub_q0p75_o48` -- температура, `vfzg_ub_q1p25` -- скорость.
У кода есть партнёр: с чем ячейка сравнивается и что подаётся на вход
карты. Если порог, названный "по температуре ОЖ", сравнивается с
оборотами -- имя неверно, как бы гладко ни сошлось выравнивание.

Словарь величин ОЗУ строится в три шага, и только первый шаг на веру:

1. ЯКОРЯ -- ячейки, разобранные по коду вручную, без дамоса
   (обороты 0xF89E, t ОЖ 0x8AB4, дроссель 0x89F3, скорость 0x881D...).
2. ПРОТЯЖКА через поиск по оси: индекс, который вернул поиск по оси
   оборотов, сам является оборотами.
3. ГОЛОСОВАНИЕ: ячейка получает величину, только если её дают не меньше
   двух разных калибровок и без единого несогласия. Карты с двумя входами
   разрешаются исключением: если один вход известен, второй получает
   оставшуюся ось.

Проверка честна в одну сторону: совпадение величины не доказывает, что
имя ТОЧНОЕ (порог "верхний" и "нижний" одной величины так не различить),
но несовпадение доказывает, что имя НЕВЕРНО.

    python3 tools/meaning.py --a2l results/FBH3ID60_legacy_all.a2l \\
        --fw firmware/FBH3ID60_stok.bin --out out/meaning.json
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "editor", "a2l"))
sys.path.insert(0, os.path.join(ROOT, "editor", "core"))

import codeuse                                              # noqa: E402
import geometry                                             # noqa: E402
import model                                                # noqa: E402

AXIS_LOOKUP = "0x0074da"
AXIS_NAME = re.compile(r"^S[A-Z0-9]{2}\d{2}[A-Z_]{2}[US][BW]$")

# Шаг 1. Разобрано по коду вручную, каждая -- с доказательством в docs/.
ANCHORS = {
    0xF89E: ("nmot", 40.0, 0.0, "0x85EFB4: обороты/160 от 0xF8A0; NMAX 26800 = 6700 об/мин"),
    0x8A9B: ("nmot", 10.0, 0.0, "0x85EFE2: обороты/40 от 0xF8A0"),
    0xF8A0: ("nmot", 0.25, 0.0, "0x85EEAC: константа/период; NMAX сходится при 0.25"),
    0x8AB4: ("temp", 0.75, -48.0, "0x8604C2: окно 13..233 и sub r4,#0x40"),
    0x8AB3: ("temp", 0.75, -48.0, "0x8604D2: копия 0x8AB4"),
    0x89F3: ("rel", 0.390625, 0.0, "0x8506CE: сравнение с WDKVLN/WDKSLN"),
    0x881D: ("vfzg", 1.25, 0.0, "0x856840: окно VKOAO/VKOAU"),
}


# Разобрано вручную: смысл выведен из кода рассуждением, которое эта
# проверка не повторит (физика, сверка с паспортом, вся цепочка вызовов).
# Ручная отметка НЕ ПЕРЕКРЫВАЕТ автоматическое противоречие: если код
# скажет другое, приговор останется "противоречит" и потребует разбора.
_MANUAL = {
    "docs/31 кондиционер": (
        "LIMTKOA TKOAMNN TKOAMXN TKOEMNN TMKOAO TMKOAU TNACMX TVKOA TVKOE "
        "VKOAO VKOAU WDKKOAN_0 WDKKOAN_1 WDKKOEN"),
    "docs/33 полная нагрузка": (
        "WDKSLN WDKVLN_0 WDKVLN_1 KLLAMFA_0 KLLAMFA_1 TV_LAMFA KFZWMN "
        "KFZWMNST RLLRTMO RLLRUN TARAU TASHS TLRHS TMRA1 TMRA2 DTC_CODES"),
    "docs/32 топливо": "KRKTE KFLBTS KFFDLBTS CWLAMBTS KFLF",
    "docs/20, 27, 34 наполнение": "RLNOT KUMSRL",
    "docs/14, 16 моментная модель, сверка с паспортным пиком 4500": (
        "KFMIRL KFMDS KFMIOP KFZWOP KFZW"),
    "docs/13 отсечка, масштаб по паспортным 6700": (
        "NMAX NMAXDV DNMAXH TNMAXDV NMXDKPU"),
    "docs/26 тракт ДМРВ, диспетчер по режимам": (
        "KFKHFM KFPU KFPUSU KFPUNW KFPUSUNW KLAF KFMSNWDK PUKANS KFRLW"),
    "docs/20, 36 подсос и адаптация расхода": "CWFKMSDKA KIMSALL MSALLMN MSALLMX MSLG",
}
MANUAL = {n: doc for doc, names in _MANUAL.items() for n in names.split()}


def ctp7_names(profile: dict) -> dict:
    """
    Карты, которые узнал ChipTuningPRO 7 (docs/35): наше имя -> имя у CTP7.

    Это сверка со стороны, независимая и от дамоса, и от моего разбора:
    CTP7 показывает те же значения и те же оси под своим названием. Как и
    ручная отметка, она НЕ ПЕРЕКРЫВАЕТ противоречие с кодом.
    """
    out = {}
    sec = profile.get("ctp7_confirmed") or {}
    out.update(sec.get("matches") or {})

    def walk(o):
        if isinstance(o, dict):
            if o.get("name") and isinstance(o.get("ctp7"), str):
                out.setdefault(o["name"], o["ctp7"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(profile)
    return out


def parse_conv(c: str):
    """'nmot_ub_q40' -> ('nmot', 40.0, 0.0). Не разобрали -- None."""
    if not c:
        return None
    parts = c.split("_")
    fam = parts[0]
    if fam in ("dez", "dezsub1", "luquant", ""):
        return None
    width = 1 if "ub" in parts or "sb" in parts else 2
    fac, off = None, 0.0
    for p in parts[1:]:
        num = p[1:].replace("p", ".")
        try:
            if p.startswith("q"):
                fac = float(num)
            elif p.startswith("b"):
                fac = float(num) / (255 if width == 1 else 65535)
            elif p.startswith("o"):
                off = -float(num)
        except ValueError:
            pass
    if fac is None:
        return (fam, None, off)
    return (fam, fac, off)


UNIT_FAM = {"rpm": "nmot", "Upm": "nmot", "U/min": "nmot", "Grad C": "temp",
            "°C": "temp", "km/h": "vfzg", "V": "spg", "s": "t",
            "grad KW": "zw"}


# Одна и та же величина под разными именами семейства в дамосе.
FAM_ALIAS = {"relDK": "rel", "spgg": "spg", "nmotd": "nmotd", "zkzw": "zk"}
# Здесь множитель -- дело представления (тики таймера, доли), а не
# величины: порог в 100-мс тиках и счётчик в 10-мс тиках сравнимы.
FREE_SCALE = ("fak", "t", "zk", "m", "dez")


def _fam(f: str) -> str:
    return FAM_ALIAS.get(f, f)


def same(a, b) -> bool:
    """Две величины совпадают по семейству и, если известно, по множителю."""
    if not a or not b:
        return False
    fa, fb = _fam(a[0]), _fam(b[0])
    if fa == fb and fa.startswith(FREE_SCALE):
        return True
    if fa != fb:
        # время в разных тиках дамос пишет по-разному: t100ms, t20ms...
        if a[0].startswith("t") and b[0].startswith("t"):
            return True
        return False
    if a[1] is None or b[1] is None:
        return True
    return abs(a[1] - b[1]) <= 0.02 * max(abs(a[1]), abs(b[1]), 1e-9)


def build_dictionary(uses, var_of, convs):
    """Словарь величин ОЗУ: якоря, протяжка по осям, голосование."""
    known = {k: (v[0], v[1], v[2]) for k, v in ANCHORS.items()}
    why = {k: "якорь: " + v[3] for k, v in ANCHORS.items()}

    for _round in range(4):
        votes = collections.defaultdict(collections.Counter)
        for addr, recs in uses.items():
            var = var_of(addr)
            if not var:
                continue
            for r in recs:
                if r["kind"] == "ptr":
                    ins = r.get("inputs") or []
                    if r.get("target") == AXIS_LOOKUP:
                        cls = convs(var, "self")
                        for i in ins:
                            if cls:
                                votes[i][cls] += 1
                        if cls and r.get("dest"):
                            votes[r["dest"]][cls] += 1
                        continue
                    axes = [c for c in (convs(var, "x"), convs(var, "y")) if c]
                    if len(ins) == 1 and len(axes) >= 1:
                        votes[ins[0]][axes[0]] += 1
                    elif len(ins) == 2 and len(axes) == 2:
                        a, b = ins
                        for p, q in ((a, b), (b, a)):
                            if p in known:
                                rest = [c for c in axes if not same(c, known[p])]
                                if len(rest) == 1:
                                    votes[q][rest[0]] += 1
                elif r["kind"] == "read" and r.get("partner") is not None:
                    cls = convs(var, "self")
                    if cls:
                        votes[r["partner"]][cls] += 1
        added = 0
        for cell, cnt in votes.items():
            if cell in known:
                continue
            classes = list(cnt)
            # согласие: все голоса одной величины
            base = classes[0]
            if all(same(c, base) for c in classes) and sum(cnt.values()) >= 2:
                known[cell] = base
                why[cell] = "голосование: %d калибровок, без несогласий" \
                    % sum(cnt.values())
                added += 1
        if not added:
            break
    return known, why


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Проверка смысла карт по коду")
    ap.add_argument("--a2l", required=True)
    ap.add_argument("--fw", required=True)
    ap.add_argument("--xfer", default=os.path.join(ROOT, "results", "damos_перенос.json"))
    ap.add_argument("--profile", default=os.path.join(ROOT, "profiles", "FBH3ID60.json"))
    ap.add_argument("--out")
    ap.add_argument("--sidecar", action="store_true",
                    help="записать рядом с A2L файл <a2l>.check.json -- его "
                         "показывает редактор в подсказке у каждой карты")
    a = ap.parse_args(argv)

    buf = open(a.fw, "rb").read()
    uses = codeuse.scan(buf)
    a2l = model.load(a.a2l)
    am = geometry.detect_addressing(a2l, len(buf))
    lay = geometry.resolve_all(a2l, buf, am)

    xvars = json.load(open(a.xfer, encoding="utf-8"))["variables"]
    xfer = {v["name"]: v for v in xvars}
    # соседи по дамосу: в каком порядке переменные лежат у донора
    order = sorted((v for v in xvars if v.get("src_addr") is not None),
                   key=lambda v: v["src_addr"])
    pos = {v["name"]: i for i, v in enumerate(order)}

    def neighbours_differ(name, mine, reach=2):
        """
        Отличаются ли соседи по величине. Если да -- совпадение величины
        доказывает и МЕСТО: сдвиг выравнивания на одну-две позиции дал бы
        соседа, а он другой величины. Если соседи той же величины, сдвиг
        внутри ряда этой проверкой не виден, и честно сказать можно только
        "величина сходится".
        """
        i = pos.get(name)
        if i is None or not mine:
            return False
        for d in range(1, reach + 1):
            for j in (i - d, i + d):
                if 0 <= j < len(order):
                    # Сосед без разбираемой величины (кодовое слово,
                    # счётчик) МЕСТО ТОЖЕ ДОКАЗЫВАЕТ: сдвинься выравнивание
                    # на него -- совпадения величины не получилось бы вовсе.
                    oj = order[j]
                    c = parse_conv(oj.get("conv", "") or (
                        oj.get("x_conv", "") if AXIS_NAME.match(oj["name"]) else ""))
                    if c is not None and same(c, mine):
                        return False
        return True

    # профиль: собственные записи, у которых пересчёт задан числами
    prof = {}

    def walk(o):
        if isinstance(o, dict):
            if o.get("name") and o.get("factor") is not None:
                prof.setdefault(o["name"], o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    profile = json.load(open(a.profile, encoding="utf-8"))
    walk(profile)
    ctp7 = ctp7_names(profile)

    def base(n):
        return re.sub(r"_[0-9A-F]{5}$", "", n)

    at = {}
    for n, L in lay.items():
        for ad in (L.data_off, L.header_off):
            if ad and ad > 0:
                at.setdefault(ad, n)

    def var_of(addr):
        return at.get(addr) or at.get(addr + 1)

    def convs(name, which):
        b = base(name)
        v = xfer.get(b)
        if v:
            if which == "self":
                # У осей дамоса собственный пересчёт ПУСТ, величина лежит в
                # x_conv (так у всех 73). Без этого ось на верном месте
                # оставалась непроверенной, а при сдвиге её соседи -- нет, и
                # сдвинутый вариант ряда начинал выигрывать.
                if not v.get("conv") and AXIS_NAME.match(b):
                    return parse_conv(v.get("x_conv", ""))
                return parse_conv(v.get("conv", ""))
            return parse_conv(v.get(which + "_conv", ""))
        p = prof.get(b)
        if p and which == "self":
            fam = UNIT_FAM.get(p.get("unit", ""))
            if fam:
                return (fam, float(p.get("factor") or 1), -float(p.get("offset") or 0))
        if p and which in ("x", "y"):
            ax = p.get(which + "_axis")
            if isinstance(ax, dict):
                fam = UNIT_FAM.get(ax.get("unit", ""))
                if fam:
                    return (fam, float(ax.get("factor") or 1),
                            -float(ax.get("offset") or 0))
        return None

    known, why = build_dictionary(uses, var_of, convs)
    uses_s = {"0x%05X" % k for k in uses}

    # -- наблюдения: что код показывает про каждую карту, без оглядки на имя
    obs = {}
    for n, L in lay.items():
        # Байт перед данными -- это счётчик точек у кривых со встроенной
        # осью, и на него смотрит УКАЗАТЕЛЬ. Но прямое чтение того же
        # байта -- это СОСЕДНИЙ скаляр: так RLO2 получал сравнения NU3 и
        # выходил "противоречащим".
        recs = list(uses.get(L.data_off, []))
        if L.header_off and L.header_off > 0:
            recs += uses.get(L.header_off, [])
        recs += [r for r in uses.get(L.data_off - 1, []) if r["kind"] == "ptr"]
        # ЗАГОЛОВОК, О КОТОРОМ ОПИСЬ НЕ ЗНАЕТ. Карта записана адресом
        # данных, а код указывает на её заголовок: так KFMDKO и KFZWMNST
        # числились "без кода", хотя читаются. Заголовок вычисляем и
        # ПРОВЕРЯЕМ по байтам -- там должны лежать размеры самой карты,
        # иначе это совпадение адресов, а не заголовок.
        # Если код сам указывает прямо на данные, заголовка у этой карты
        # нет: байты перед ней принадлежат СОСЕДУ. Без этого правила WEAN
        # получал обращения к оси температуры, лежащей перед ним, а
        # SNM06LHUB -- к предыдущей оси.
        direct = any(r["kind"] == "ptr" for r in uses.get(L.data_off, []))
        if not (L.header_off and L.header_off > 0) and not direct:
            nx, ny = L.nx, L.ny
            # Один байт, равный числу точек, -- не доказательство: байт 8
            # встречается где угодно, и так к WEAN подцепился указатель на
            # соседнюю карту. Требуем ещё и строго возрастающие оси.
            def rising(a0, k):
                v = buf[a0:a0 + k]
                return len(v) == k and all(v[i] < v[i + 1] for i in range(k - 1))
            cand = []
            if ny > 1:
                h = L.data_off - 2 - nx - ny
                if h >= 0 and sorted(buf[h:h + 2]) == sorted((nx, ny)):
                    k1 = buf[h]
                    k2 = buf[h + 1]
                    if rising(h + 2, k1) and rising(h + 2 + k1, k2):
                        cand.append(h)
            elif nx > 2:
                h = L.data_off - 1 - nx
                if h >= 0 and buf[h] == nx and rising(h + 1, nx):
                    cand.append(h)
            for h in cand:
                recs += [r for r in uses.get(h, []) if r["kind"] == "ptr"]
        # обращения ВНУТРЬ тела: база таблицы с индексом, чтение ячейки
        # из середины. Адрес они подтверждают, смысл -- нет.
        body = any(("0x%05X" % ad) in uses_s
                   for ad in range(L.data_off + 1, max(L.end, L.data_off + 1)))
        o = []
        for r in recs:
            if r["kind"] == "read" and r.get("partner") in known:
                o.append(("self", known[r["partner"]], r["site"], r["partner"]))
            elif r["kind"] == "ptr":
                # Ось сравнивается СО СВОЕЙ величиной: вход поиска по оси
                # оборотов -- это обороты. Раньше вход оси сверялся с её
                # "осями", которых у оси нет, и шестьдесят осей висели без
                # проверки.
                is_axis = bool(AXIS_NAME.match(base(n))) \
                    or r.get("target") == AXIS_LOOKUP
                for i in r.get("inputs") or []:
                    if i in known:
                        o.append(("self" if is_axis else "axis",
                                  known[i], r["site"], i))
        obs[n] = (o, bool(recs) or body)

    def score(name, o):
        """Сколько наблюдений имя объясняет и скольким противоречит."""
        ok = bad = 0
        ev = []
        mine = convs(name, "self")
        axes = [c for c in (convs(name, "x"), convs(name, "y")) if c]
        for kind, cls, site, cell in o:
            if kind == "self":
                if not mine:
                    continue
                hit = same(mine, cls)
                ev.append("0x%06X сравнивается с 0x%04X (%s)%s"
                          % (site, cell, cls[0],
                             "" if hit else ", а по имени " + mine[0]))
            else:
                if not axes:
                    continue
                hit = any(same(c, cls) for c in axes)
                ev.append("0x%06X вход 0x%04X (%s)%s"
                          % (site, cell, cls[0], "" if hit else
                             ", а оси по имени " + "/".join(c[0] for c in axes)))
            ok += hit
            bad += not hit
        return ok, bad, ev

    # -- ряды: перенос из дамоса делается целыми сериями с одним сдвигом
    #
    # Если доказан сдвиг ряда, доказано место всех его членов -- и тех, к
    # кому код не обращается вовсе. Сдвиг считается доказанным, только
    # если при нём смысл сходится лучше, чем при сдвиге на одну-две
    # позиции в любую сторону, и нет ни одного противоречия.
    name2map = {}
    for n in lay:
        name2map.setdefault(base(n), n)
    runs, cur, last = [], [], None
    for v in order:
        if v.get("addr") is None:
            continue
        d = v["addr"] - v["src_addr"]
        if cur and d == last:
            cur.append(v["name"])
        else:
            if cur:
                runs.append(cur)
            cur = [v["name"]]
        last = d
    if cur:
        runs.append(cur)

    run_of = {}
    run_ok = {}
    inside = {}
    for ri, members in enumerate(runs):
        here = [m for m in members if m in name2map]
        if len(here) < 2:
            continue
        sc = {}
        for sh in (-2, -1, 0, 1, 2):
            ok = bad = 0
            for i, m in enumerate(members):
                if m not in name2map:
                    continue
                j = i + sh
                if not 0 <= j < len(members):
                    continue
                a_, b_, _ = score(members[j], obs[name2map[m]][0])
                ok += a_
                bad += b_
            sc[sh] = (ok, bad)
        ok0, bad0 = sc[0]
        best_other = max(ok - 2 * bad for sh, (ok, bad) in sc.items() if sh)
        confirmed = bad0 == 0 and ok0 >= 2 and ok0 > best_other
        # ВИЛКА. Ряд в двести переменных, подтверждённый сорока
        # наблюдениями, ещё не доказывает место каждого: в тихом
        # промежутке, где код молчит, раскладка могла съехать локально.
        # Поэтому место доказано только у тех, кто зажат между двумя
        # проверенными членами не дальше GAP переменных друг от друга.
        GAP = 20
        anchors = [i for i, m in enumerate(members) if m in name2map
                   and score(m, obs[name2map[m]][0])[0] > 0]
        braced = set()
        for x, y in zip(anchors, anchors[1:]):
            if y - x <= GAP:
                braced.update(range(x, y + 1))
        if len(anchors) == 1:
            braced.add(anchors[0])
        for i, m in enumerate(members):
            if m in name2map:
                run_of[name2map[m]] = ri
                inside[name2map[m]] = i in braced
        run_ok[ri] = (confirmed, sc, len(here))

    # -- приговор каждой карте
    verdicts = {}
    for n, L in lay.items():
        o, has_code = obs[n]
        ok, bad, ev = score(n, o)
        ri = run_of.get(n)
        in_good_run = ri is not None and run_ok[ri][0] and inside.get(n)
        if bad:
            v = "противоречит"
        elif ok and neighbours_differ(base(n), convs(n, "self")):
            v = "смысл и место сходятся"
        elif in_good_run:
            v = "место доказано рядом"
        elif ok:
            v = "величина сходится"
        elif has_code:
            v = "адрес из кода, смысл нечем проверить"
        else:
            v = "кода нет"
        man = MANUAL.get(n) if not re.search(r"_[0-9A-F]{5}$", n) else None
        # ТОЛЬКО по точному имени. По имени без суффикса адреса CTP7
        # доставался и отвергнутому двойнику: ETALAM_10515, WDKSLN_18FCB
        # ходили в "подтверждено CTP7", хотя CTP7 видел совсем другой адрес.
        c7 = ctp7.get(n)
        if c7 and v not in ("противоречит", "смысл и место сходятся"):
            ev = ["CTP7: " + c7, "автоматическая проверка: " + v] \
                + (["разобрано вручную: " + man] if man else []) + ev
            v = "подтверждено CTP7"
        elif man and v not in ("противоречит", "смысл и место сходятся"):
            ev = ["разобрано вручную: " + man,
                  "автоматическая проверка: " + v] + ev
            v = "разобрано вручную"
        rec = {"verdict": v, "ok": ok, "bad": bad, "evidence": ev[:6]}
        if ri is not None:
            rec["run"] = {"members": len(runs[ri]), "confirmed": run_ok[ri][0],
                          "scores": {str(k): list(x) for k, x in run_ok[ri][1].items()}}
        verdicts[n] = rec

    if a.sidecar:
        side = os.path.splitext(a.a2l)[0] + ".check.json"
        with open(side, "w", encoding="utf-8") as fh:
            json.dump({n: {"verdict": v["verdict"], "evidence": v["evidence"][:3]}
                       for n, v in sorted(verdicts.items())},
                      fh, ensure_ascii=False, indent=0)
        print("записано %s" % side)

    cnt = collections.Counter(v["verdict"] for v in verdicts.values())
    print("словарь ОЗУ: %d ячеек (%d якорей)" % (len(known), len(ANCHORS)))
    for k, v in cnt.most_common():
        print("  %-38s %d" % (k, v))
    if a.out:
        json.dump({"ram": {"0x%04X" % k: {"class": list(v), "why": why[k]}
                           for k, v in sorted(known.items())},
                   "maps": verdicts},
                  open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("записано %s" % a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
