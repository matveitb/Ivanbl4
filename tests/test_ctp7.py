#!/usr/bin/env python3
"""
Сверка с ChipTuningPRO 7: экспорт .cte против нашей разметки.

CTP7 -- независимый источник: он не знает ни нашего дамоса, ни моего
выравнивания. Если его таблица по значениям и осям совпала с нашей
картой, место и смысл подтверждены со стороны. Здесь закреплено, что
совпало, -- и что проверка умеет НЕ совпадать: одиночная величина по
значению не опознаётся, а "по правке" -- только если байты и правда
менялись.
"""

import contextlib
import io
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "editor", "a2l"))
sys.path.insert(0, os.path.join(ROOT, "editor", "core"))

import ctematch                                             # noqa: E402
import geometry                                             # noqa: E402
import mapaccess as M                                       # noqa: E402
import model                                                # noqa: E402

CT = os.path.join(ROOT, "firmware", "ctp7")
CTE = os.path.join(CT, "FBH3ID60-E2_Mod_by_Mpower.bin.cte")
BIN = os.path.join(CT, "FBH3ID60-E2_Mod_by_Mpower_test.bin")
BASE = os.path.join(ROOT, "firmware", "FBH3ID60_mpower.bin")
A2L = os.path.join(ROOT, "results", "FBH3ID60_legacy_all.a2l")
MAIN = os.path.join(ROOT, "results", "FBH3ID60_legacy.a2l")

fails = []


def check(cond, msg):
    print(("OK   " if cond else "ОШИБКА ") + msg)
    if not cond:
        fails.append(msg)


BIN2 = os.path.join(CT, "FBH3ID60-E2_Mod_by_Mpower_test2.bin")
CTE2 = os.path.join(CT, "FBH3ID60-E2_Mod_by_Mpower_test2.cte")
STOCK = os.path.join(ROOT, "firmware", "FBH3ID60_stok.bin")


def run(base, bin_=BIN, cte=CTE, base_cte=None):
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "m.json")
        argv = ["--bin", bin_, "--a2l", A2L, "--json", out, cte]
        if base:
            argv[:0] = ["--base", base]
        if base_cte:
            argv[:0] = ["--base-cte", base_cte]
        with contextlib.redirect_stdout(io.StringIO()):
            ctematch.main(argv)
        return {r["ctp7"]: r for r in json.load(open(out, encoding="utf-8"))}


def main():
    if not os.path.exists(CTE):
        print("нет %s -- пропуск" % CTE)
        return 0

    cs = ctematch.parse_cte(CTE)
    check(len(cs) == 71, "в экспорте CTP7 разобрано таблиц: %d" % len(cs))

    r = run(BASE)

    def verdict(name):
        return r[name]["verdict"]

    # -- по значениям и осям
    for ctp, ours in [
        ("Коэфф.холодного пуска", "KST_COLD"),
        ("Множитель для коэфф.холодного пуска", "KST_COLD_MUL"),
        ("Множитель для коэфф.холодного пуска по RPM", "KST_COLD_NMUL"),
        ("Коэфф.горячего пуска", "KST_HOT"),
        ("Множитель для коэфф.горячего пуска", "KST_HOT_MUL"),
        ("Обедняющий коэфф. повторного пуска", "KST_RESTART"),
        ("Коррекция нелинейности системы подачи топлива", "KF_TI_NONLIN"),
        ("Коэффициент пленки при обогащении", "KFABAK"),
        ("Коэффициент пленки при обеднении", "KFAVAK"),
        ("Коэффициент обогащения при ускорении", "KFBAKL"),
        ("Коэффициент обеднения при замедлении", "KFVAKL"),
        ("Коэффициент убывания обогащения (Long)", "KABAKL_L"),
        ("Коэффициент убывания обеднения (Long)", "KVAKL_L"),
        ("Фаза завершения впрыска", "KFWEE"),
        ("Фаза завершения впрыска холодного двигателя", "KFWEEK"),
        ("Топливная эффективность", "ETALAM"),
        ("Коэфф. демпфирования для фильтрации момента", "KFDMDFA"),
        ("Оптимальный момент", "KFMIOP"),
        ("Порог по дросселю для вкл. адаптации", "CURVE_19515"),
    ]:
        check(verdict(ctp).startswith("СОВПАЛО: " + ours + ",")
              and "оси совпали" in verdict(ctp),
              "%s = %s, значения и оси (%s)" % (ctp, ours, verdict(ctp)))
    check(verdict("Тарировка ДМРВ") == "СОВПАЛО: HFM_LIN",
          "тарировка ДМРВ = HFM_LIN, все 512 точек")

    # -- после исправления ориентации обе карты защиты компонентов сходятся
    #    сами, без транспонирования (в MPower KFLBTS -- сплошная 1.000, её
    #    держат оси; KFFDLBTS -- "лесенка" из 0 и 1)
    check(verdict("Состав смеси для защиты нейтрализатора") == "СОВПАЛО: KFLBTS, оси совпали",
          "KFLBTS: %s" % verdict("Состав смеси для защиты нейтрализатора"))
    check(verdict("Множитель корр. ALF для защиты нейтрализатора")
          == "СОВПАЛО: KFFDLBTS, оси совпали",
          "KFFDLBTS = множитель ALF: %s" % verdict("Множитель корр. ALF для защиты нейтрализатора"))

    # -- KFZW: значения сошлись, а ось нагрузки у CTP7 соседняя (0x1014A,
    #    кончается на 90 %), код берёт 0x10157 (до 99.75 %). Если когда-нибудь
    #    "совпадут оси" -- значит, мы переехали на чужую ось.
    check(verdict("Базовый УОЗ") == "СОВПАЛО: KFZW, ОСИ НЕ СОВПАЛИ",
          "KFZW: значения те же, ось нагрузки у CTP7 другая -- наша по коду")

    # -- обратная величина: CTP7 рисует коэффициент фильтра как tau = 2.55 / байт
    for ctp, ours in [("Постоянная времени фильтра", "KFZMDFA"),
                      ("Постоянная времени фильтра (fuel restart)", "KFZMDFAS"),
                      ("Постоянная времени фильтра (low clutch torque)", "KFZMDFAU")]:
        check(verdict(ctp).startswith("СОВПАЛО КАК ОБРАТНАЯ ВЕЛИЧИНА: %s, у CTP7 = 2.55 /" % ours),
              "%s = 2.55 / %s" % (ctp, ours))

    # -- одиночные величины -- по правке владельца (firmware/ctp7/v1.txt)
    for ctp, ours in [
        ("Обороты отключения топливоподачи", "NMAX"),
        ("Обороты отключения топливоподачи при неиспр. ДС", "NMAXDV"),
        ("Температура двиг. для включения адаптации", "TMOT_ADAPT"),
        ("Условие выхода из регулирования (ISS=0)", "TMRA1"),
        ("Условие выхода из регулирования (ISS=1)", "TMRA2"),
        ("Мин. обороты для диагностики пропусков воспламенения", "NMIN_MISF"),
        ("Макс. обороты для диагностики пропусков воспламенения", "NMAX_MISF"),
        ("Порог Твозд. для включения вентилятора", "TAN_FAN"),
        ("Порог ТОЖ для включения вентилятора, режим 5", "TMOT_FAN5"),
        ("Гистерезис ТОЖ", "TMOT_FAN_HYS"),
        ("Смещение тарировки ДМРВ", "HFM_LIN_OFS"),
    ]:
        check(verdict(ctp).startswith("СОВПАЛО ПО ПРАВКЕ: " + ours + " "),
              "%s = %s по правке" % (ctp, ours))

    got = sum(1 for v in r.values() if v["verdict"].startswith("СОВПАЛО"))
    check(got >= 56, "таблиц CTP7 опознано: %d из %d" % (got, len(r)))

    # -- а теперь то, что проверка обязана НЕ найти
    check(verdict("Макс. богатая смесь").startswith("ПО ЗНАЧЕНИЮ НЕ ОПОЗНАТЬ"),
          "одиночная 0.703 не прилипает ни к чему: %s" % verdict("Макс. богатая смесь"))
    check(verdict("Порог ТОЖ для включения вентилятора, режим 1")
          .startswith("ПО ЗНАЧЕНИЮ НЕ ОПОЗНАТЬ"),
          "непоправленная одиночная величина по значению не опознаётся")
    same = run(BIN)        # "до правки" = та же прошивка: правок нет вовсе
    n_ed = sum(1 for v in same.values() if "ПО ПРАВКЕ" in v["verdict"])
    check(n_ed == 0, "без правок ничего не опознано 'по правке': %d" % n_ed)

    # -- второй раунд: владелец поправил в CTP7 всё, что по значению не
    #    различалось (firmware/ctp7/FBH3ID60-E2_Mod_by_Mpower_test2.*)
    r2 = run(BIN, BIN2, CTE2, CTE)
    for ctp, ours in [
        ("Порог по дросселю для режима полной нагрузки", "WDKVLN_0"),
        ("Порог по дросселю для режима полной нагрузки 2", "WDKVLN_1"),
        ("Состав смеси в режиме полной мощности", "KLLAMFA_0"),
        ("Состав смеси в режиме полной мощности 2", "KLLAMFA_1"),
        ("Минимальный расчетный УОЗ", "KFZWMS"),
        ("Мин. открытие дросселя, обеспечивающее макс. наполнение", "WDKUGDN"),
        ("Макс. богатая смесь", "LAM_RICH_MAX"),
        ("Коррекция фазы впрыска", "WEEM"),
        ("Коэффициент убывания обогащения (Short)", "KABAKL_S"),
        ("Коррекция сост.смеси при неактивном лямбда-регулировании", "KL_LAM_OPEN"),
        ("Порог ТОЖ для включения вентилятора, режим 1", "TMOT_FAN1"),
        ("Порог ТОЖ для включения вентилятора, режим 2", "TMOT_FAN2"),
        ("Порог ТОЖ для включения вентилятора, режим 3", "TMOT_FAN3"),
        ("Порог ТОЖ для включения вентилятора, режим 4", "TMOT_FAN4"),
    ]:
        check(r2[ctp]["verdict"].startswith("СОВПАЛО ПО ПРАВКЕ: " + ours + " "),
              "раунд 2: %s = %s" % (ctp, ours))
    check(r2["Состав смеси на частичных нагрузках"]["verdict"]
          == "СОВПАЛО ПО ПРАВКЕ: KFLF (байты изменились, у CTP7 = 128 / байт)",
          "KFLF: CTP7 показывает 128/байт -- 1/лямбда")
    for ctp, ours in [("Множитель корр. ALF для защиты нейтрализатора", "KFFDLBTS"),
                      ("Состав смеси при прогреве, L-регулирование активно", "KF_LAM_WARM"),
                      ("Порог включения обогащения при ускорении", "DTH_ACC_ENR"),
                      ("Порог включения обеднения при замедлении", "DTH_DEC_LEAN")]:
        check(r2[ctp]["verdict"] == "СОВПАЛО: %s, оси совпали" % ours,
              "раунд 2: %s = %s" % (ctp, ours))
    # TMOT_FAN3 поставили на 143.25 -- ровно столько, сколько стоит в
    # ISS=0. Байты изменились у порога вентилятора, а таблица ISS=0 у CTP7
    # осталась прежней: это НЕ правка ISS=0.
    check(not r2["Условие выхода из регулирования (ISS=0)"]["verdict"].startswith("СОВПАЛО"),
          "ISS=0 не прилипает к поправленному на то же значение TMOT_FAN3: %s"
          % r2["Условие выхода из регулирования (ISS=0)"]["verdict"])
    both = sum(1 for k in r if r[k]["verdict"].startswith("СОВПАЛО")
               or r2[k]["verdict"].startswith("СОВПАЛО"))
    check(both >= 70, "за два раунда опознано: %d из %d" % (both, len(r)))
    clash = [k for k in r if r[k]["verdict"].startswith("СОВПАЛО")
             and r2[k]["verdict"].startswith("СОВПАЛО") and r[k]["ours"] != r2[k]["ours"]]
    check(not clash, "раунды не спорят между собой: %s" % clash)

    # -- ориентация всех карт, которые код вызывает с заголовком, -- по коду
    import orient                                           # noqa: E402
    stock = open(STOCK, "rb").read()
    a2l_all = model.load(A2L)
    lay_s = geometry.resolve_all(a2l_all, stock,
                                 geometry.detect_addressing(a2l_all, len(stock)))
    by_data = {L.data_off: (n, L) for n, L in lay_s.items()}
    wrong = []
    for site, tgt, d, h in orient.calls(stock):
        if d in by_data:
            n, L = by_data[d]
            if L.ny > 1 and L.nx != L.ny and stock[h] != L.nx:
                wrong.append(n)
    check(not wrong, "все карты читаются той стороной, что и в коде: %s" % (wrong or "да"))

    # -- плёнка читается правильной стороной: подряд 7 точек по оборотам
    buf = open(BIN, "rb").read()
    a2l = model.load(MAIN)
    lay = geometry.resolve_all(a2l, buf, geometry.detect_addressing(a2l, len(buf)))
    L = lay["KFABAK"]
    first = [round(v * 256) for v in M.read_phys(buf, L)[0]]
    check((L.nx, L.ny) == (7, 9) and first == [121, 121, 119, 114, 109, 106, 104],
          "KFABAK: 7 по оборотам на 9 по температуре, первая строка %s" % first)
    check(L.y_axis.values(buf)[-1] == 98.25 and L.x_axis.values(buf)[-1] == 4200,
          "оси KFABAK: температура до 98.25, обороты до 4200")

    # -- отозванное под CTP7 не вернулось
    names = set(lay)
    for gone in ("MSNTATE", "KFAGRS", "MAP_130_11698_12", "MAP_131_116B2_6x6"):
        check(gone not in names, "%s нет в описании" % gone)

    print()
    if fails:
        print("ПРОВАЛЕНО: %d" % len(fails))
        return 1
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
