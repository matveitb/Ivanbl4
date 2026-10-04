#!/usr/bin/env python3
"""
ctematch -- сопоставить экспорт калибровок CTP7 (.cte) с нашей разметкой.

.cte -- текстовый экспорт ChipTuningPRO 7, в духе INI, кодировка cp1251:

    [66613309]                 ; хеш карты
    Name=Базовый УОЗ
    X1Z1=15,75                 ; значения в физических единицах
    ...
    QX1=680                    ; точки оси X (у CTP7 -- первая ось)
    QZ1=9,75                   ; точки оси Z
    Const=7900                 ; у одиночных величин

Адресов в нём нет. Но есть ЗНАЧЕНИЯ и ОСИ, а прошивка, из которой сделан
экспорт, у нас есть. Значит, каждую карту CTP7 можно найти среди наших по
содержимому: то же число ячеек, те же значения, те же оси. Это проверка
со стороны, независимая и от дамоса, и от моего выравнивания, -- и для
всех карт CTP7 разом, без поштучной правки каждой.

Совпадение ищется в два прохода:

1. ТОЧНОЕ -- физические значения совпали (до шага квантования). Значит,
   совпали и адрес, и масштаб.
2. С ТОЧНОСТЬЮ ДО МНОЖИТЕЛЯ -- значения пропорциональны. Адрес тот же, а
   масштаб у нас и у CTP7 разный; множитель печатается, и один из нас
   неверно пересчитывает.

Карты, где все значения одинаковы (например, сплошная 1.000), по значениям
неразличимы -- для них решают оси; если и оси не решают, кандидаты
перечисляются честно, а не выбирается первый попавшийся.

    python3 tools/ctematch.py --bin firmware/ctp7/FBH3ID60-E2_Mod_by_Mpower_test.bin \\
        --base firmware/FBH3ID60_mpower.bin \\
        firmware/ctp7/FBH3ID60-E2_Mod_by_Mpower.bin.cte

Одиночная величина по значению не опознаётся ("201" найдётся где угодно), но
владелец поправил одиннадцать таких величин в CTP7 и записал, что менял. С
--base из совпавших по значению кандидатов берётся тот, чьи байты изменились.

CTP7 показывает некоторые таблицы не в масштабе прошивки, а через обратную
величину: коэффициент фильтра момента KFZMDFA он рисует как постоянную
времени, 2.55 / байт. Такое совпадение печатается отдельно.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "editor", "a2l"))
sys.path.insert(0, os.path.join(ROOT, "editor", "core"))

import geometry                                             # noqa: E402
import mapaccess as M                                       # noqa: E402
import model                                                # noqa: E402


def num(s: str) -> float:
    return float(s.strip().replace(",", "."))


def parse_cte(path: str) -> list:
    """Разобрать .cte в список карт: имя, значения (матрица), оси."""
    text = open(path, "rb").read().decode("cp1251")
    parts = re.split(r"^\[([0-9A-Fa-f]{8})\]\s*$", text, flags=re.M)
    out = []
    for i in range(1, len(parts), 2):
        kv = {}
        for line in parts[i + 1].splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                kv[k.strip()] = v.strip()
        if "Name" not in kv:
            continue
        rec = {"hash": parts[i], "name": kv["Name"]}
        try:
            if "Const" in kv:
                rec["values"] = [[num(kv["Const"])]]
            else:
                qx = [num(kv["QX%d" % j]) for j in range(1, 999) if "QX%d" % j in kv]
                qz = [num(kv["QZ%d" % j]) for j in range(1, 999) if "QZ%d" % j in kv]
                if any(re.match(r"^X\d+Z\d+$", k) for k in kv):
                    nx = max(int(m.group(1)) for k in kv
                             for m in [re.match(r"^X(\d+)Z\d+$", k)] if m)
                    nz = max(int(m.group(1)) for k in kv
                             for m in [re.match(r"^X\d+Z(\d+)$", k)] if m)
                    # строка -- индекс X, столбец -- индекс Z
                    rec["values"] = [[num(kv["X%dZ%d" % (x, z)])
                                      for z in range(1, nz + 1)]
                                     for x in range(1, nx + 1)]
                else:
                    n = max(int(k[1:]) for k in kv if re.match(r"^X\d+$", k))
                    rec["values"] = [[num(kv["X%d" % x]) for x in range(1, n + 1)]]
                rec["qx"], rec["qz"] = qx, qz
        except (KeyError, ValueError):
            continue
        out.append(rec)
    return out


def flat(m):
    return [v for row in m for v in row]


def close(a, b, tol):
    return all(abs(x - y) <= tol * max(1.0, abs(x), abs(y)) for x, y in zip(a, b))


def ratio(a, b):
    """Постоянный множитель b/a, если он есть."""
    rs = [y / x for x, y in zip(a, b) if abs(x) > 1e-9]
    if len(rs) < max(2, len(a) // 2) or any(abs(x) <= 1e-9 and abs(y) > 1e-6
                                           for x, y in zip(a, b)):
        return None
    r0 = sum(rs) / len(rs)
    if r0 == 0 or any(abs(r - r0) > 0.01 * abs(r0) for r in rs):
        return None
    return r0


def inverse(raw, b):
    """
    Постоянное произведение raw * b, если оно есть: CTP7 показывает
    коэффициент фильтра момента как постоянную времени, tau = 2.55 / байт.
    Сравнивается с СЫРЫМ байтом -- так константа выходит круглой.
    """
    if len(raw) < 4 or any(x == 0 for x in raw):
        return None
    ps = [x * y for x, y in zip(raw, b)]
    p0 = sum(ps) / len(ps)
    if p0 == 0 or any(abs(q - p0) > 0.002 * abs(p0) for q in ps):
        return None
    return p0


def locate(buf: bytes, c: dict) -> str:
    """
    Найти карту CTP7 прямо в байтах, если у нас её нет.

    Масштаб угадывается по шагу значений: у байтовой карты с множителем
    0.75 все значения кратны 0.75. Смещение пробуется нулевое и 48 (так
    хранится температура). Ищутся байтовое и словное представление, в
    исходном порядке и транспонированное. Находка годится, только если
    встречается в калибровке РОВНО ОДИН раз -- иначе это не адрес, а
    совпадение.
    """
    rows = c["values"]
    if len(rows) * len(rows[0]) < 4:
        return ""
    orders = [flat(rows)]
    if len(rows) > 1:
        orders.append([rows[r][q] for q in range(len(rows[0]))
                       for r in range(len(rows))])
    vals = orders[0]
    uniq = sorted(set(round(v, 9) for v in vals))
    if len(uniq) < 3:
        return ""
    steps = [b - a for a, b in zip(uniq, uniq[1:]) if b - a > 1e-9]
    base_steps = {min(steps)}
    for f in (0.75, 0.5, 0.25, 0.1, 1.0, 1 / 128, 1 / 256, 1 / 1024, 40.0,
              10.0, 0.390625, 0.0234375, 0.01, 0.05, 0.02, 1 / 32768, 1 / 65536):
        base_steps.add(f)
    found = []
    for f in sorted(base_steps):
        for off in (0.0, 48.0):
            for w in (1, 2):
                for oi, seq in enumerate(orders):
                    raw = [(v + off) / f for v in seq]
                    if any(abs(r - round(r)) > 0.02 for r in raw):
                        continue
                    ri = [int(round(r)) for r in raw]
                    lim = 256 if w == 1 else 65536
                    if any(not -lim // 2 <= r < lim for r in ri):
                        continue
                    pat = b"".join((r % lim).to_bytes(w, "little") for r in ri)
                    pos = [i for i in range(0x10000, 0x20000 - len(pat))
                           if buf[i:i + len(pat)] == pat]
                    if len(pos) == 1:
                        found.append("0x%05X (%s, x%g%s%s)" % (
                            pos[0], "байт" if w == 1 else "слово", f,
                            ", -%g" % off if off else "",
                            ", транспонирована" if oi else ""))
    found = sorted(set(found))
    return "; ".join(found[:2]) if len(found) <= 2 else ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="CTP7 .cte против нашей разметки")
    ap.add_argument("cte")
    ap.add_argument("--bin", required=True, help="прошивка, из которой сделан экспорт")
    ap.add_argument("--a2l", default=os.path.join(ROOT, "results",
                                                   "FBH3ID60_legacy_all.a2l"))
    ap.add_argument("--base",
                    help="прошивка ДО правки в CTP7: одиночная величина, которую "
                         "правили, опознаётся по тому, что её байты изменились")
    ap.add_argument("--base-cte",
                    help="экспорт CTP7 той прошивки, что в --base. С ним правка "
                         "засчитывается, только если таблица CTP7 тоже изменилась "
                         "и связь с нашей картой держится в обеих выгрузках")
    ap.add_argument("--json")
    a = ap.parse_args(argv)

    buf = open(a.bin, "rb").read()
    base = open(a.base, "rb").read() if a.base else None
    base_cte = {c["name"]: c for c in parse_cte(a.base_cte)} if a.base_cte else None
    a2l = model.load(a.a2l)
    am = geometry.detect_addressing(a2l, len(buf))
    lay = geometry.resolve_all(a2l, buf, am)
    ru = {}
    try:
        ru = json.load(open(os.path.splitext(a.a2l)[0] + ".ru.json", encoding="utf-8"))
    except Exception:                                       # noqa: BLE001
        pass

    ours = {}
    for n, L in lay.items():
        try:
            vals = M.read_phys(buf, L)
        except Exception:                                   # noqa: BLE001
            continue
        try:
            xs = L.x_axis.values(buf) if L.x_axis.count > 1 else []
            ys = L.y_axis.values(buf) if L.y_axis.count > 1 else []
        except Exception:                                   # noqa: BLE001
            xs, ys = [], []
        ours[n] = (vals, xs, ys, L)

    report = []
    for c in parse_cte(a.cte):
        cv = c["values"]
        cf = flat(cv)
        rows, cols = len(cv), len(cv[0])
        uniform = max(cf) - min(cf) < 1e-9
        exact, scaled, inv = [], [], []
        for n, (vals, xs, ys, L) in ours.items():
            if L.nx * L.ny != len(cf):
                continue
            # наша матрица: строки = ny, столбцы = nx. У CTP7 строки -- X.
            # Пробуем как есть и транспонированной.
            cands = []
            if (L.ny, L.nx) == (rows, cols):
                cands.append(("", flat(vals)))
            if (L.nx, L.ny) == (rows, cols) and rows != cols or \
                    (L.nx, L.ny) == (rows, cols) and L.ny > 1:
                cands.append(("транспонирована", [vals[r][q] for q in range(L.nx)
                                                  for r in range(L.ny)]))
            if L.ny == 1 and rows == 1:
                cands = [("", vals[0])]
            for how, ov in cands:
                if len(ov) != len(cf):
                    continue
                # Допуск АБСОЛЮТНЫЙ, в полшага квантования. Относительный
                # допуск от множителя у осей оборотов (x40) выходил больше
                # единицы, и "совпадало" что угодно -- так "коэффициент
                # холодного пуска" прилипал к оси SNM12ESUB.
                #
                # Но и не шире той точности, с которой CTP7 печатает числа
                # (оси -- до десятых): при множителе 1 полшага -- это 0.5, и
                # "макс. богатая смесь" 0.703 прилипала к битовому полю,
                # равному 1.
                half = abs(L.factor) * 0.51 + 1e-6
                if all(abs(x - y) <= min(half, max(0.06, 0.01 * abs(y)))
                       for x, y in zip(ov, cf)):
                    exact.append((n, how))
                    break
                r = ratio(ov, cf)
                if r and abs(r - 1) > 0.01:
                    scaled.append((n, how, r))
                    break
                f = L.factor or 1.0
                k = inverse([round((x + L.offset) / f) for x in ov], cf)
                if k:
                    inv.append((n, how, k))
                    break

        # оси решают, если по значениям неоднозначно
        def axis_ok(n):
            vals, xs, ys, L = ours[n]
            qa = [q for q in (c.get("qx"), c.get("qz")) if q]
            ax = [q for q in (xs, ys) if q]
            if not qa:
                return None
            hits = 0
            for q in qa:
                if any(len(q) == len(o) and close(q, o, 0.02) for o in ax):
                    hits += 1
            return hits == len(qa)

        if len(exact) > 1:
            by_axis = [e for e in exact if axis_ok(e[0])]
            if by_axis:
                exact = by_axis

        # Одиночная величина или карта из одинаковых чисел по значению НЕ
        # опознаётся: "201" найдётся где угодно, сплошная 1.000 -- тоже.
        # Так "Смещение тарировки ДМРВ" прилипало к DNWEK, а "режим 4
        # вентилятора" -- к TMKOAO. Такие признаём только по оси.
        # Правка решает там, где значение не решает: из кандидатов годится
        # тот, чьи байты отличаются от прошивки до правки, и только если
        # такой один.
        #
        # Одного этого мало. Во втором раунде "ISS=0" = 143.25 прилипло к
        # порогу вентилятора: его как раз поставили на 143.25, а сам ISS=0
        # не трогали. Поэтому с --base-cte требуется ещё, чтобы изменилась и
        # таблица CTP7, а связь (то же значение или обратная величина)
        # держалась в ОБЕИХ выгрузках -- до правки и после.
        def edited(n):
            L = ours[n][3]
            return base is not None and buf[L.data_off:L.end] != base[L.data_off:L.end]

        before = base_cte.get(c["name"]) if base_cte else None
        ctp_changed = base_cte is None or (before is not None
                                           and flat(before["values"]) != cf)

        def relation(L):
            """'exact', ('inverse', k) или None -- по обеим выгрузкам сразу."""
            pairs = [(buf, cf)]
            if before is not None:
                pairs.append((base, flat(before["values"])))
            ph, rw, tg = [], [], []
            for b_, t_ in pairs:
                try:
                    v = flat(M.read_phys(b_, L))
                except Exception:                           # noqa: BLE001
                    return None
                if len(v) != len(t_):
                    return None
                # ориентация у однородной таблицы не важна, а у прежней
                # выгрузки сверяем как набор чисел
                v, t_ = sorted(v), sorted(t_)
                ph += v
                tg += t_
                f = L.factor or 1.0
                rw += [round((x + L.offset) / f) for x in v]
            half = abs(L.factor) * 0.51 + 1e-6
            if all(abs(x - y) <= min(half, max(0.06, 0.01 * abs(y))) for x, y in zip(ph, tg)):
                return "exact"
            n_ = len(cf)
            parts = [(rw[i:i + n_], tg[i:i + n_][::-1]) for i in range(0, len(rw), n_)]
            k = inverse(sum((p[0] for p in parts), []), sum((p[1] for p in parts), []))
            return ("inverse", k) if k else None

        if (len(cf) == 1 or uniform) and base is not None and ctp_changed:
            hits = []
            for n, (vals, xs, ys, L) in ours.items():
                if L.nx * L.ny == len(cf) and edited(n):
                    rel = relation(L)
                    if rel:
                        hits.append((n, rel))
            if len(hits) == 1:
                n, rel = hits[0]
                verdict = "СОВПАЛО ПО ПРАВКЕ: %s (байты изменились, %s)" % (
                    n, "значение сходится" if rel == "exact"
                    else "у CTP7 = %.4g / байт" % rel[1])
                print("%-58s %s" % (c["name"][:58], verdict))
                report.append({"ctp7": c["name"], "hash": c["hash"],
                               "verdict": verdict, "ours": [n]})
                continue

        if (len(cf) == 1 or uniform) and exact:
            ax_ok = [e for e in exact if axis_ok(e[0])]
            if len(ax_ok) == 1 and len(cf) > 1:
                exact = ax_ok
            else:
                verdict = ("ПО ЗНАЧЕНИЮ НЕ ОПОЗНАТЬ (%s); совпадает с: %s" % (
                    "одна величина" if len(cf) == 1 else "все значения одинаковы",
                    ", ".join(e[0] for e in exact[:4])))
                print("%-58s %s" % (c["name"][:58], verdict))
                report.append({"ctp7": c["name"], "hash": c["hash"],
                               "verdict": verdict, "ours": []})
                continue
        if uniform:
            scaled, inv = [], []

        if len(exact) == 1:
            n, how = exact[0]
            ok = axis_ok(n)
            verdict = "СОВПАЛО: %s%s%s" % (n, " (" + how + ")" if how else "",
                                           "" if ok is None else
                                           (", оси совпали" if ok else ", ОСИ НЕ СОВПАЛИ"))
        elif len(exact) > 1:
            verdict = "НЕОДНОЗНАЧНО (%s): %s" % (
                "все значения одинаковы" if uniform else "одинаковые карты",
                ", ".join(e[0] for e in exact[:6]))
        elif scaled:
            n, how, r = scaled[0]
            verdict = "АДРЕС ТОТ ЖЕ, МАСШТАБ ДРУГОЙ: %s, у CTP7 в %.4g раза больше" % (n, r)
        elif len(inv) == 1:
            n, how, k = inv[0]
            verdict = "СОВПАЛО КАК ОБРАТНАЯ ВЕЛИЧИНА: %s, у CTP7 = %.4g / байт%s" % (
                n, k, " (" + how + ")" if how else "")
        else:
            hit = locate(buf, c)
            verdict = ("НЕТ У НАС, найдено в байтах: " + hit) if hit \
                else "НЕ НАЙДЕНО ни среди наших карт, ни в байтах"
        best = exact[0][0] if len(exact) == 1 else (scaled[0][0] if scaled and not exact else
                                                     (inv[0][0] if len(inv) == 1 and not exact else None))
        print("%-58s %s" % (c["name"][:58], verdict))
        if best and ru.get(best):
            print("%-58s   у нас: %s" % ("", ru[best]))
        report.append({"ctp7": c["name"], "hash": c["hash"], "verdict": verdict,
                       "ours": [e[0] for e in exact] or [s[0] for s in scaled]
                       or [i[0] for i in inv]})

    if a.json:
        json.dump(report, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
