#!/usr/bin/env python3
"""
Проверка «смеси по УДК»: чтение логов, расчёт, запись.

Настоящего лога с известным ответом нет ни у кого, поэтому главная
проверка идёт на модели мотора (editor/core/udksim.py), у которой ошибка
ДМРВ задана заранее:

1. программа обязана НАЙТИ заложенную ошибку -- по узлам тарировки;
2. та же модель на ИСПРАВЛЕННОЙ прошивке обязана дать желаемую смесь --
   это проверка не формулы, а результата, ради которого всё затевалось;
3. два лога с разными нулями часов обязаны свестись сами.

Плюс то, что ломается тихо: разбор форматов логов, кодировки, десятичная
запятая, отбраковка, пределы шага, контрольные суммы и отмена.
"""

import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "editor", "a2l"))
sys.path.insert(0, os.path.join(ROOT, "editor", "core"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import logio            # noqa: E402
import project as P     # noqa: E402
import saving           # noqa: E402
import udk              # noqa: E402
import udksim           # noqa: E402

A2L = os.path.join(ROOT, "results", "FBH3ID60_legacy.a2l")
STOCK = os.path.join(ROOT, "firmware", "FBH3ID60_stok.bin")
V2M = os.path.join(ROOT, "firmware", "FBH3ID60_v2M.bin")

fails = []


def check(cond, msg):
    print(("OK   " if cond else "ОШИБКА ") + msg)
    if not cond:
        fails.append(msg)


def fl_lambda(fm, truth, lo=2500, hi=6000):
    """Средняя лямбда в цилиндре на полном газе прогретого мотора."""
    v = [s.lam for s in truth if fm.full_load(s.rpm, s.tps)
         and lo <= s.rpm <= hi and s.ect >= 70 and s.lam < 2
         and not s.enrich]
    return sum(v) / len(v), min(v), max(v)


def drive(fm, sensor, offset=12.3, seed=1, ltft=0.0):
    tr = udksim.run(fm, sensor, seed=seed, ltft=ltft)
    ecu = logio.parse_text(udksim.ecu_log_carscanner(tr, seed=seed + 1),
                           "ecu.csv")
    wb = logio.parse_text(udksim.wb_log(tr, offset=offset, seed=seed + 2),
                          "wb.csv")
    return tr, ecu, wb


def solve_true(q_tab, err):
    """Какой настоящий расход даёт табличный q_tab при ошибке err."""
    q = q_tab
    for _ in range(50):
        q = q_tab * err(q)
    return q


def test_formats():
    print("-- форматы логов")
    cs = ('"SECONDS";"PID";"VALUE";"UNITS"\n'
          '"10.512";"Engine RPM";"812";"rpm"\n'
          '"10.734";"Mass air flow rate";"3,21";"g/s"\n'
          '"11.020";"Engine RPM";"830";"rpm"\n'
          '"11.300";"Mass air flow rate";"3.40";"g/s"\n')
    L = logio.parse_text(cs)
    rpm = L.channels.get("Engine RPM")
    maf = L.channels.get("Mass air flow rate")
    check(L.fmt == "long" and rpm and maf and rpm.v == [812, 830],
          "Car Scanner: длинный формат, каналы по имени параметра")
    check(maf.v == [3.21, 3.40] and abs(rpm.t[1] - rpm.t[0] - 0.508) < 1e-9
          and rpm.t[0] == 10.512,
          "Car Scanner: десятичная запятая, время как в файле")

    tq = ("GPS Time, Device Time, Longitude, Engine RPM(rpm), "
          "Mass Air Flow Rate(g/s), Throttle Position(Manifold)(%), "
          "Relative Throttle Position(%), "
          "Fuel Trim Bank 1 Short Term(%), Air Fuel Ratio(Measured)(:1)\n"
          "-,18-Jun-2014 23:59:59.500,-,812,3.2,12.5,2,-,14.7\n"
          "-,19-Jun-2014 00:00:00.000,-,-,3.3,12.9,2,1.6,14.6\n"
          "-,19-Jun-2014 00:00:00.500,-,850,3.5,13.1,2,0.8,14.5\n")
    L = logio.parse_text(tq)
    ch = L.channels
    check(L.fmt == "wide" and L.t0_kind == "epoch"
          and ch["Engine RPM"].t == [0.0, 1.0],
          "Torque: время прибора с датой, через полночь, прочерки пропущены")
    check(ch["Mass Air Flow Rate"].unit == "g/s",
          "Torque: единицы вынуты из скобок")
    roles = udk.detect_roles(L, None)
    check(roles["tps"] == ("ecu", "Throttle Position(Manifold)"),
          "дроссель: взят абсолютный, а не относительный")
    check(roles["lam"] == ("ecu", "Air Fuel Ratio(Measured)"),
          "ШДК внутри лога ЭБУ найден: %s" % (roles["lam"],))
    k, kind = udk.lam_scale(ch["Air Fuel Ratio(Measured)"])
    check(kind == "AFR" and abs(14.7 * k - 1) < 1e-9,
          "AFR распознан и переводится в лямбду делением на 14.7")

    lw = ("LogWorks3 export\nDate: 2026-10-01\n\n"
          "Time\tLC-2 Lambda\tAFR\n0,0\t1,000\t14,70\n0,1\t0,98\t14,4\n"
          "0,2\t0,95\t13,97\n")
    L = logio.parse_text(lw)
    check(list(L.channels) == ["LC-2 Lambda", "AFR"]
          and L.channels["LC-2 Lambda"].v == [1.0, 0.98, 0.95],
          "LogWorks: шапка пропущена, табуляция и десятичная запятая")
    check(udk.detect_roles(L, None)["lam"] == ("ecu", "LC-2 Lambda"),
          "из лямбды и AFR одного логгера взята лямбда")

    ru = ("Время;Обороты, об/мин;Расход воздуха, кг/ч;Дроссель, %;"
          "УДК, AFR;Температура ОЖ, °C\n"
          "12:00:00,0;800;12,1;2,0;14,7;88\n"
          "12:00:00,5;820;12,4;2,4;14,8;88\n")
    L = logio.parse_text(logio.decode(ru.encode("cp1251")))
    roles = udk.detect_roles(L, None)
    check(L.t0_kind == "day" and all(roles[r] for r in
                                      ("rpm", "maf", "tps", "lam", "ect")),
          "русский лог в cp1251: все роли найдены по-русски")
    k, unit = udk.maf_scale(L.channels["Расход воздуха"])
    check(k == 1.0 and unit == "кг/ч", "расход в кг/ч понят по подписи")

    cs = ('"SECONDS";"PID";"VALUE";"UNITS";"LATITUDE";"LONGTITUDE";\n'
          '"86399.5";"Engine RPM";"800";"rpm";"";"";\n'
          '"0.5";"Engine RPM";"810";"rpm";"";"";\n')
    L = logio.parse_text(cs)
    t = L.channels["Engine RPM"].t
    check(t[1] - t[0] == 1.0,
          "Car Scanner: секунды от полуночи, переход через полночь, "
          "лишние столбцы координат")

    L = logio.parse_text("rpm;maf\n800;3,1\n810;3,2\n820;3,3\n", dt=0.25)
    check(L.channels["rpm"].t == [0.0, 0.25, 0.5] and "шаг" in L.note,
          "без столбца времени -- по номеру строки, и это сказано")

    ch = logio.Channel("x", "", [0.0, 1.0, 10.0], [0.0, 10.0, 100.0])
    check(logio.sample(ch, 0.5, 2.0) == 5.0, "выборка: линейно между")
    check(logio.sample(ch, 5.0, 2.0) is None,
          "выборка: через дыру в 9 с значение не выдумывается")


def test_model():
    print("-- модель блока")
    ps = P.Project.open(A2L, STOCK)
    fs = udk.FuelModel.from_project(ps)
    check(not fs.full_load(3000, 60) and fs.full_load(3000, 70),
          "сток: полная нагрузка с 65 %% дросселя (WDKVLN %.1f)"
          % fs.wdkvln_at(3000))
    check(abs(fs.rl(2000, 123.4) - 100) < 0.5,
          "наполнение по формуле блока: 2000 об/мин, 123.4 кг/ч -> %.1f %%"
          % fs.rl(2000, 123.4))
    check(fs.hfm_index(0.0) is None and fs.hfm_index(-5) is None,
          "нулевой и обратный расход в тарировке не ищется")
    i1, i2 = fs.hfm_index(50.0), fs.hfm_index(300.0)
    check(i1 < i2 and abs(fs.hfm_flow(int(round(i1))) - 50) < 1.0,
          "номер точки тарировки по расходу: 50 кг/ч -> %.1f (%.2f В)"
          % (i1, fs.hfm_volts(i1)))
    check(abs(fs.hfm_index(0.01) * 2 * 5 / 1023 - 1.0) < 0.02,
          "ноль расхода -- около 1.0 В, как у HFM5")

    pv = P.Project.open(A2L, V2M)
    fv = udk.FuelModel.from_project(pv)
    lam, fl = fv.lam_cmd(4000, 100, 85)
    check(fl and abs(fv.kllamfa_at(4000) - 1.0234) < 0.001 and lam == 1.0,
          "v2M: KLLAMFA 1.023, но цель на полном газе 1.000 (минимум)")
    check(fv.full_load(3000, 21) and not fv.full_load(3000, 19),
          "v2M: полная нагрузка уже с 20 % дросселя")


def test_offset_and_hfm():
    print("-- поправка ДМРВ на модели с известной ошибкой")
    p = P.Project.open(A2L, V2M)
    fm = udk.FuelModel.from_project(p)
    sensor = list(fm.hfm_raw)
    err = udksim.err_lean_top
    tr, ecu, wb = drive(fm, sensor)
    roles = udk.detect_roles(ecu, wb)
    check(roles["lam"] == ("wb", "Лямбда") and roles["stft"]
          and roles["ect"], "роли: ШДК из своего лога, коррекции и ОЖ из ELM")

    st = udk.Settings(target="hfm")
    off, how = udk.find_offset(ecu, wb, roles, st)
    check(off is not None and abs(off - 12.3) < 0.25,
          "сдвиг логов найден сам: %.2f с при заложенных 12.30 (%s)"
          % (off or 0, how))
    st.offset = off

    before, _, worst = fl_lambda(fm, tr)
    res = udk.analyze(fm, ecu, wb, roles, st)
    check(res.rejected["холодный мотор"] > 0
          and res.rejected["дроссель закрыт, сброс топлива"] > 0
          and res.rejected["рывок дросселя"] > 0,
          "отбракованы прогрев, накат и рывки: %s"
          % ", ".join("%s %.0f с" % kv for kv in res.rejected.items() if kv[1]))
    check(any("KLLAMFA выше 1.000" in n for n in res.notes),
          "сказано, что KLLAMFA 1.023 у v2M не действует")

    good = [n for n in res.nodes if n.status == "по данным"
            and fm.hfm_flow(int(n.x)) > 60]
    devs = []
    for n in good:
        q_tab = fm.hfm_flow(int(n.x))
        want = err(solve_true(q_tab, err))
        devs.append(abs(n.new - want))
    check(len(good) >= 6 and max(devs) < 0.02,
          "ошибка ДМРВ найдена в %d узлах, расхождение с заложенной не "
          "больше %.1f %%" % (len(good), max(devs) * 100 if devs else 99))
    low = [n for n in res.nodes if n.status == "по данным"
           and 0 < fm.hfm_flow(int(n.x)) < 110]
    check(low and all(abs(n.new - 1) < 0.01 for n in low),
          "там, где ДМРВ не врёт (по коррекциям STFT), поправки нет")
    new = res.hfm_new
    check(all(new[i] >= new[i - 1] for i in range(1, len(new))),
          "новая тарировка неубывающая")
    check(all(new[i] == fm.hfm_raw[i] for i in range(len(new))
              if fm.hfm_flow(i) <= 0),
          "участок нулевого и обратного расхода не тронут")

    orig = bytes(p.buf)
    eds = udk.apply(p, res)
    check(p.changed_maps() == {"HFM_LIN"},
          "записано только в HFM_LIN: %s" % sorted(p.changed_maps()))

    fm2 = udk.FuelModel.from_project(p)
    tr2 = udksim.run(fm2, sensor, ltft=0.0)
    after, lo2, hi2 = fl_lambda(fm2, tr2)
    check(worst > 1.10 and abs(after - 1.0) < 0.02 and hi2 < 1.03,
          "та же езда на исправленной прошивке: λ на полном газе было до "
          "%.3f, стало %.3f (%.3f…%.3f), цель 1.000" % (worst, after, lo2, hi2))

    tmp = tempfile.mkdtemp(prefix="udk")
    try:
        out = os.path.join(tmp, "out.bin")
        r = p.save(out)
        bad, total = saving.verify(open(out, "rb").read(), r.table)
        check(bad == 0 and total > 0,
              "сохранено с суммами: неверных %d из %d" % (bad, total))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    while p.history.can_undo:
        p.undo()
    check(bytes(p.buf) == orig and len(eds) == 1,
          "отмена вернула образ побайтово")


def test_kllamfa():
    print("-- лямбда полной нагрузки на модели")
    p = P.Project.open(A2L, V2M)
    fm = udk.FuelModel.from_project(p)
    sensor = list(fm.hfm_raw)
    tr, ecu, wb = drive(fm, sensor, offset=-4.0, seed=11)
    roles = udk.detect_roles(ecu, wb)
    st = udk.Settings(target="kllamfa", lam_want=0.88)
    st.offset, _ = udk.find_offset(ecu, wb, roles, st)
    check(abs(st.offset + 4.0) < 0.25,
          "отрицательный сдвиг тоже находится: %.2f" % st.offset)
    res = udk.analyze(fm, ecu, wb, roles, st)
    check(all(n.new <= n.old for n in res.nodes)
          and res.nodes[-1].new < 0.85,
          "KLLAMFA: " + " ".join("%.0f:%.3f" % (n.x, n.new)
                                 for n in res.nodes))
    check(all(abs(n.new * 128 - round(n.new * 128)) < 1e-9
              for n in res.nodes),
          "в отчёте то, что ляжет в файл: шаг 1/128")
    udk.apply(p, res)
    check(p.changed_maps() == {"KLLAMFA_0", "KLLAMFA_1"},
          "записано в оба варианта KLLAMFA и больше никуда")
    fm2 = udk.FuelModel.from_project(p)
    tr2 = udksim.run(fm2, sensor, ltft=0.0)
    after, lo2, hi2 = fl_lambda(fm2, tr2)
    check(abs(after - 0.88) < 0.015 and lo2 > 0.85 and hi2 < 0.91,
          "та же езда с новой KLLAMFA: λ на полном газе %.3f (%.3f…%.3f), "
          "хотели 0.880" % (after, lo2, hi2))


def test_keep_target():
    print("-- KLLAMFA: цель как в прошивке")
    p = P.Project.open(A2L, V2M)
    fm = udk.FuelModel.from_project(p)
    sensor = list(fm.hfm_raw)
    tr, ecu, wb = drive(fm, sensor, seed=21)
    roles = udk.detect_roles(ecu, wb)
    st = udk.Settings(target="kllamfa")
    check(st.lam_want is None, "по умолчанию цель не задаётся, берётся из прошивки")
    st.offset, _ = udk.find_offset(ecu, wb, roles, st)
    res = udk.analyze(fm, ecu, wb, roles, st)
    check("как в прошивке" in udk.report(res), "в отчёте сказано, откуда цель")
    udk.apply(p, res)
    fm2 = udk.FuelModel.from_project(p)
    after, lo2, hi2 = fl_lambda(fm2, udksim.run(fm2, sensor, ltft=0.0))
    check(abs(after - 1.0) < 0.015 and hi2 < 1.03,
          "v2M: цель 1.000 осталась, ошибку ДМРВ на полном газе закрыла "
          "KLLAMFA -- λ %.3f (%.3f…%.3f)" % (after, lo2, hi2))


def test_closed_at_wot():
    print("-- v2M: регулятор по зонду работает и на полном газе (по коду)")
    p = P.Project.open(A2L, V2M)
    fm = udk.FuelModel.from_project(p)
    sensor = list(fm.hfm_raw)
    tr = udksim.run(fm, sensor, ltft=0.0, by_code=True)
    wot = [s for s in tr if fm.full_load(s.rpm, s.tps) and s.rpm > 4500
           and not s.enrich and s.ect >= 70]
    lam_wot = sum(s.lam for s in wot) / len(wot)
    stft_wot = sum(s.stft for s in wot) / len(wot)
    check(abs(lam_wot - 1.0) < 0.02 and stft_wot > 8,
          "ШДК на полном газе почти 1.000 (%.3f) -- ошибку прячет STFT %+.1f %%"
          % (lam_wot, stft_wot))
    ecu = logio.parse_text(udksim.ecu_log_carscanner(tr, seed=31), "ecu.csv")
    wb = logio.parse_text(udksim.wb_log(tr, offset=0.0, seed=32), "wb.csv")
    roles = udk.detect_roles(ecu, wb)
    res = udk.analyze(fm, ecu, wb, roles, udk.Settings(target="hfm"))
    udk.apply(p, res)
    fm2 = udk.FuelModel.from_project(p)
    tr2 = udksim.run(fm2, sensor, ltft=0.0, by_code=True)
    wot2 = [s for s in tr2 if fm2.full_load(s.rpm, s.tps) and s.rpm > 4500
            and not s.enrich and s.ect >= 70]
    stft2 = sum(s.stft for s in wot2) / len(wot2)
    check(abs(stft2) < 3.0,
          "после поправки ДМРВ STFT на полном газе %+.1f %% вместо %+.1f %%: "
          "ошибка найдена через коррекцию, а не через ШДК" % (stft2, stft_wot))
    # регулятор упёрся (предел 10 %): ШДК видит остаток, коррекция -- свою
    # часть, вместе -- вся ошибка
    tr3 = udksim.run(fm, sensor, ltft=0.0, by_code=True, stft_max=10.0)
    wot3 = [s for s in tr3 if fm.full_load(s.rpm, s.tps) and s.rpm > 5500
            and not s.enrich and s.ect >= 70]
    check(sum(s.lam for s in wot3) / len(wot3) > 1.04,
          "коррекция упёрлась в 10 %% -- ШДК показывает бедно: %.3f"
          % (sum(s.lam for s in wot3) / len(wot3)))
    ecu = logio.parse_text(udksim.ecu_log_carscanner(tr3, seed=33), "ecu.csv")
    wb = logio.parse_text(udksim.wb_log(tr3, offset=0.0, seed=34), "wb.csv")
    res = udk.analyze(fm, ecu, wb, udk.detect_roles(ecu, wb),
                      udk.Settings(target="hfm"))
    top = [n for n in res.nodes if n.status == "по данным"
           and fm.hfm_flow(int(n.x)) > 250]
    check(top and all(n.new > 1.10 for n in top),
          "и тогда ошибка найдена целиком: %s" % " ".join(
              "%.3f" % n.new for n in top))


def test_guards():
    print("-- пределы и неполные логи")
    p = P.Project.open(A2L, V2M)
    fm = udk.FuelModel.from_project(p)
    sensor = list(fm.hfm_raw)

    # ДМРВ ЗАВЫШАЕТ на 30 %: обеднять можно не больше max_lean за раз
    def over(q):
        return 1.0 / (1.0 + 0.3 * min(1.0, max(0.0, (q - 120) / 100)))
    tr = udksim.run(fm, sensor, err=over)
    ecu = logio.parse_text(udksim.ecu_log_carscanner(tr), "ecu.csv")
    wb = logio.parse_text(udksim.wb_log(tr, offset=0.0), "wb.csv")
    roles = udk.detect_roles(ecu, wb)
    st = udk.Settings(target="hfm", max_lean=10.0)
    res = udk.analyze(fm, ecu, wb, roles, st)
    check(min(n.new for n in res.nodes) >= 0.9 - 1e-9
          and any("предел" in n.status for n in res.nodes),
          "обеднение ограничено 10 %% за проход: минимум %.3f"
          % min(n.new for n in res.nodes))

    # без коррекций: для ДМРВ только полная нагрузка, и это сказано
    tr = udksim.run(fm, sensor)
    ecu = logio.parse_text(udksim.ecu_log_carscanner(tr), "ecu.csv")
    for name in [n for n in ecu.channels if "trim" in n]:
        del ecu.channels[name]
    wb = logio.parse_text(udksim.wb_log(tr, offset=0.0), "wb.csv")
    roles = udk.detect_roles(ecu, wb)
    res = udk.analyze(fm, ecu, wb, roles, udk.Settings(target="hfm"))
    check(all(s.fl for s in res.used) and
          any("STFT в логе нет" in w for w in res.warnings),
          "без STFT в поправку ДМРВ идёт только полный газ, с предупреждением")

    # обязательного канала нет -- понятная ошибка, а не падение
    del ecu.channels["Throttle position"]
    roles = udk.detect_roles(ecu, wb)
    try:
        udk.analyze(fm, ecu, wb, roles, udk.Settings())
        check(False, "без дросселя расчёт обязан отказаться")
    except ValueError as exc:
        check("Дроссель" in str(exc), "без дросселя: «%s»" % exc)

    # ШДК внутри лога ЭБУ (один широкий лог) -- сдвиг не нужен
    tr = udksim.run(fm, sensor)
    rows = ["Time;Engine RPM;MAF (g/s);TPS (%);STFT (%);LTFT (%);"
            "ECT (C);Wideband AFR"]
    for s in tr[::10]:
        rows.append(("%.2f;%.1f;%.2f;%.1f;%.1f;%.1f;%.0f;%.2f" % (
            s.t, s.rpm, s.ml / 3.6, s.tps, s.stft, s.ltft, s.ect,
            min(s.lam, 1.9) * 14.7)).replace(".", ","))
    one = logio.parse_text("\n".join(rows))
    roles = udk.detect_roles(one, None)
    check(roles["lam"] == ("ecu", "Wideband AFR"),
          "один общий лог: ШДК найден в нём же")
    res = udk.analyze(fm, one, None, roles,
                      udk.Settings(target="kllamfa", lag=0.0))
    check(res.bands and res.bands[-1].err > 1.1,
          "по общему логу видно обеднение на верхах: %+.1f %% на %d об/мин"
          % ((res.bands[-1].err - 1) * 100, res.bands[-1].lo))


def nb_log(tr, seed, pids=None):
    """Лог Car Scanner без ШДК: обороты, расход, дроссель, коррекции, УДК."""
    return logio.parse_text(udksim.ecu_log_carscanner(
        tr, seed=seed, pids=pids or udksim.PIDS_NB), "ecu.csv")


def wot_trims(fm, tr, rpm=5000):
    """Что на верхах доливают коррекции: (1+STFT)(1+LTFT), среднее."""
    v = [(1 + s.stft / 100) * (1 + s.ltft / 100) for s in tr
         if fm.full_load(s.rpm, s.tps) and s.rpm > rpm and s.closed
         and not s.enrich and s.ect >= 70]
    return sum(v) / len(v)


def test_narrowband():
    print("-- по УДК: родной узкий зонд, без ШДК")
    p = P.Project.open(A2L, V2M)
    fm = udk.FuelModel.from_project(p)
    sensor = list(fm.hfm_raw)
    err = udksim.err_lean_top
    tr = udksim.run(fm, sensor, ltft=3.0, by_code=True, nb_ctrl=True)
    before = wot_trims(fm, tr)
    ecu = nb_log(tr, 41)
    roles = udk.detect_roles(ecu, None)
    check(roles["lam"] is None and roles["o2v"] == ("ecu", "O2 sensor 1 voltage")
          and roles["stft"] and roles["ltft"],
          "роли: ШДК нет, УДК (вольты) и коррекции найдены")
    res = udk.analyze(fm, ecu, None, roles, udk.Settings(target="hfm"))
    check(res.narrow and "по УДК" in udk.report(res),
          "расчёт сам перешёл на коррекции, в отчёте так и сказано")
    check(res.rejected.get("контур только что замкнулся", 0) > 0
          and "ШДК вне диапазона" not in res.rejected,
          "после замыкания контура STFT не верим, пока догоняет")
    good = [n for n in res.nodes if n.status == "по данным"
            and fm.hfm_flow(int(n.x)) > 60]
    devs = [abs(n.new - err(solve_true(fm.hfm_flow(int(n.x)), err)))
            for n in good]
    check(len(good) >= 6 and max(devs) < 0.025,
          "ошибка ДМРВ найдена по одним коррекциям: %d узлов, расхождение до "
          "%.1f %%" % (len(good), max(devs) * 100 if devs else 99))
    low = [n for n in good if fm.hfm_flow(int(n.x)) < 110]
    check(low and all(abs(n.new - 1) < 0.015 for n in low),
          "где ДМРВ не врёт, LTFT +3 %% не принят за его ошибку: %s"
          % " ".join("%.3f" % n.new for n in low))
    hi = [b for b in res.bands if b.lo >= 5000 and b.closed > 0]
    check(hi and all(b.err > 1.10 and b.stft > 8 for b in hi),
          "на верхах видно: STFT %s при λ 1.000 в цилиндре"
          % " ".join("%+.1f%%" % b.stft for b in hi))

    udk.apply(p, res)
    fm2 = udk.FuelModel.from_project(p)
    tr2 = udksim.run(fm2, sensor, ltft=3.0, by_code=True, nb_ctrl=True)
    after = wot_trims(fm2, tr2)
    check(before > 1.08 and abs(after - 1.0) < 0.025,
          "на исправленной прошивке коррекциям на полном газе доливать "
          "нечего: было %+.1f %%, стало %+.1f %%"
          % ((before - 1) * 100, (after - 1) * 100))

    try:
        udk.analyze(fm, ecu, None, roles, udk.Settings(target="kllamfa"))
        check(False, "KLLAMFA без ШДК обязан отказаться")
    except ValueError as exc:
        check("без ШДК" in str(exc) and "HFM_LIN" in str(exc),
              "KLLAMFA без ШДК -- отказ с причиной и что делать вместо")
    bare = nb_log(tr, 42)
    for name in [n for n in bare.channels if "trim" in n]:
        del bare.channels[name]
    try:
        udk.analyze(fm, bare, None, udk.detect_roles(bare, None),
                    udk.Settings())
        check(False, "без ШДК и без STFT расчёт обязан отказаться")
    except ValueError as exc:
        check("ни ШДК, ни" in str(exc), "без ШДК и без STFT: понятный отказ")


def test_saturation():
    print("-- по УДК: коррекция в упоре, несколько проходов")
    p = P.Project.open(A2L, V2M)
    fm = udk.FuelModel.from_project(p)
    sensor = list(fm.hfm_raw)
    st = udk.Settings(target="hfm", stft_lim=8.0)
    passes = []
    for k in range(2):
        cur = udk.FuelModel.from_project(p)
        tr = udksim.run(cur, sensor, ltft=3.0, by_code=True, nb_ctrl=True,
                        stft_max=8.0)
        res = udk.analyze(cur, nb_log(tr, 51 + k), None,
                          udk.detect_roles(nb_log(tr, 51 + k), None), st)
        passes.append((wot_trims(cur, tr), res))
        if k == 0:
            top = [n for n in res.nodes if n.status.startswith("упор")]
            check(top and all(fm.hfm_flow(int(n.x)) > 200 for n in top)
                  and any("упоре" in w for w in res.warnings),
                  "предел STFT 8 %%: упор найден на %d верхних узлах, в "
                  "предупреждении -- нужен ещё проход" % len(top))
            check(res.bands[-1].sat > 0.5,
                  "в полосах полного газа виден упор: %.0f %% времени"
                  % (res.bands[-1].sat * 100))
        udk.apply(p, res)
    tr = udksim.run(udk.FuelModel.from_project(p), sensor, ltft=3.0,
                    by_code=True, nb_ctrl=True, stft_max=8.0)
    final = wot_trims(udk.FuelModel.from_project(p), tr)
    wot = [s for s in tr if fm.full_load(s.rpm, s.tps) and s.rpm > 5000
           and s.closed and not s.enrich]
    lam = sum(s.lam for s in wot) / len(wot)
    check(abs(final - 1.0) < 0.025 and abs(lam - 1.0) < 0.01,
          "за два прохода: на верхах λ %.3f, коррекции доливают %+.1f %%"
          % (lam, (final - 1) * 100))


def test_open_loop():
    print("-- по УДК: контур на полном газе разомкнут")
    p = P.Project.open(A2L, V2M)
    fm = udk.FuelModel.from_project(p)
    # как принято думать про Bosch: на полной нагрузке регулятор выключен,
    # STFT сброшен в 0
    tr = udksim.run(fm, list(fm.hfm_raw), ltft=3.0, nb_ctrl=True)
    ecu = nb_log(tr, 61, udksim.PIDS_NB + [("Fuel system status", "")])
    roles = udk.detect_roles(ecu, None)
    check(roles["closed"] == ("ecu", "Fuel system status"),
          "PID 03 распознан как признак контура")
    res = udk.analyze(fm, ecu, None, roles, udk.Settings(target="hfm"))
    fl = [b for b in res.bands if b.lo >= 4000]
    check(fl and all(b.closed < 0.2 for b in fl)
          and res.rejected["контур разомкнут"] > 10,
          "полный газ не принят за λ 1.000 со STFT 0: разомкнуто %.0f с"
          % res.rejected["контур разомкнут"])
    check(any("контур был разомкнут" in w and "KLLAMFA" in w
              for w in res.warnings)
          and fl[-1].o2lean is not None and fl[-1].o2lean > 0.9,
          "сказано, что верха так не видно и что делать; зонд там беден "
          "%.0f %% времени" % ((fl[-1].o2lean or 0) * 100))
    # без PID 03 то же самое ловится по STFT, сброшенному в ноль
    ecu = nb_log(tr, 62)
    res2 = udk.analyze(fm, ecu, None, udk.detect_roles(ecu, None),
                       udk.Settings(target="hfm"))
    check(res2.rejected["контур разомкнут"] > 0.8 * res.rejected["контур разомкнут"],
          "без PID 03 разомкнутый контур найден по STFT = 0: %.0f с"
          % res2.rejected["контур разомкнут"])
    check(udk._is_closed(2, "status") and not udk._is_closed(4, "status")
          and udk._is_closed(0x0200, "status") and udk._is_closed(1, "flag"),
          "PID 03: 2 -- замкнут, 4 -- нет; DID 1216: 1 -- замкнут")


def main():
    test_formats()
    test_model()
    test_offset_and_hfm()
    test_kllamfa()
    test_keep_target()
    test_closed_at_wot()
    test_guards()
    test_narrowband()
    test_saturation()
    test_open_loop()
    print()
    if fails:
        print("ПРОВАЛЕНО: %d" % len(fails))
        return 1
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
