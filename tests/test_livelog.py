#!/usr/bin/env python3
"""
Проверка логгера: ELM, УДК, запись, сквозной прогон до поправки.

Машины в проверке нет, поэтому собеседники -- имитаторы из
editor/core/fakecar.py: ELM327 v1.5, который отвечает текстом как
настоящий (эхо, «SEARCHING...», «NO DATA», «?»), и УДК, который шлёт
пакеты Innovate ISP2. Часы виртуальные: каждый запрос к ELM двигает
время на задержку K-линии, и пять минут езды проходят за доли секунды.

Главная проверка -- сквозная: логгер пишет файл, тот же разбор, что
читает логи Car Scanner, читает его, и расчёт находит заложенную в
модель ошибку ДМРВ. Если где-то по дороге потеряется время, порядок или
единицы, она не сойдётся.
"""

import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "editor", "a2l"))
sys.path.insert(0, os.path.join(ROOT, "editor", "core"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import elm as E         # noqa: E402
import fakecar          # noqa: E402
import livelog          # noqa: E402
import logio            # noqa: E402
import obd              # noqa: E402
import project as P     # noqa: E402
import udk              # noqa: E402
import udksim           # noqa: E402
import wideband as W    # noqa: E402

A2L = os.path.join(ROOT, "results", "FBH3ID60_legacy.a2l")
V2M = os.path.join(ROOT, "firmware", "FBH3ID60_v2M.bin")

fails = []


def check(cond, msg):
    print(("OK   " if cond else "ОШИБКА ") + msg)
    if not cond:
        fails.append(msg)


def test_elm_grammar():
    print("-- разбор ответов ELM")
    txt = "010C1\r SEARCHING...\r41 0C 1A F8 \r\r>"
    lines = E.clean_lines(txt, "010C1")
    check(lines == ["41 0C 1A F8"], "эхо и SEARCHING вычищены: %s" % lines)
    rows = E.data_bytes(lines)
    check(E.find_reply(rows, 1, 0x0C, 2) == [0x1A, 0xF8],
          "обороты: 1AF8 -> %s" % E.find_reply(rows, 1, 0x0C, 2))
    check(obd.PIDS[0x0C].decode(bytes([0x1A, 0xF8])) == 1726.0,
          "1AF8 / 4 = 1726 об/мин")
    rows = E.data_bytes(E.clean_lines("BUS INIT: ...OK\r410C1AF8\r\r>"))
    check(E.find_reply(rows, 1, 0x0C, 2) == [0x1A, 0xF8],
          "без пробелов и с BUS INIT")
    rows = E.data_bytes(E.clean_lines("84 F1 10 41 0C 1A F8 C4\r\r>"))
    check(E.find_reply(rows, 1, 0x0C, 2) == [0x1A, 0xF8],
          "с заголовками KWP: «84 F1 10 41 0C ...»")
    rows = E.data_bytes(E.clean_lines(
        "84F1184 10C04D2xx\r84F110410C1AF8C4\r\r>".replace("xx", "00")))
    check(E.find_reply(rows, 1, 0x0C, 2, src=0x10) == [0x1A, 0xF8],
          "ответили коробка (18) и двигатель (10) -- взят двигатель")
    rows = E.data_bytes(E.clean_lines("48 6B 10 41 0C 1A F8 C2\r\r>"))
    check(E.find_reply(rows, 1, 0x0C, 2, src=0x10) == [0x1A, 0xF8],
          "заголовок ISO 9141: «48 6B 10 ...»")
    rows = E.data_bytes(E.clean_lines("83 F1 10 41 0C 1A F8 C3\r\r>"))
    check(E.find_reply(rows, 1, 0x0C, 2) == [0x1A, 0xF8],
          "длина в заголовке не сходится -- данные берутся по факту")
    rows = E.data_bytes(E.clean_lines("85F11062123501E7xx\r\r>".replace(
        "xx", "AA")))
    check(E.find_reply(rows, 0x22, [0x12, 0x35], 2) == [0x01, 0xE7],
          "режим 22: «62 12 35 01 E7» -> АЦП 0x1E7")
    lines = E.clean_lines("NO DATA\r\r>")
    check(E.is_error(lines) == "NO DATA"
          and E.find_reply(E.data_bytes(lines), 1, 0x0C, 2) is None,
          "NO DATA -- ошибка, данных нет")
    check(E.is_error(E.clean_lines("?\r\r>")) == "?", "«?» -- не понял")
    check(E.find_reply(E.data_bytes(["41 0C 1A"]), 1, 0x0C, 2) is None,
          "обрезанный ответ не принимается")
    # пример из стандарта (SAE J1979, он же в описании PID в Википедии)
    sup = obd.supported_from_bitmaps({0x00: [0xBE, 0x1F, 0xA8, 0x13]})
    check({0x01, 0x03, 0x04, 0x05, 0x06, 0x07, 0x0C, 0x0D, 0x0E, 0x0F,
           0x10, 0x11, 0x13, 0x15, 0x1C, 0x1F, 0x20} == sup,
          "битовая карта BE 1F A8 13 -> %s" % " ".join(
              "%02X" % p for p in sorted(sup)))
    check(obd.bitmap_for(sup, 0x00) == bytes([0xBE, 0x1F, 0xA8, 0x13]),
          "и обратно")
    for pid in (0x03, 0x05, 0x06, 0x07, 0x0C, 0x10, 0x11):
        v = {0x03: 2, 0x05: 88, 0x06: -3.125, 0x07: 4.6875, 0x0C: 3000.25,
             0x10: 45.67, 0x11: 100 * 51 / 255}[pid]
        got = obd.PIDS[pid].decode(obd.encode(pid, v))
        check(abs(got - v) < 1e-9, "PID %02X туда и обратно: %g" % (pid, got))


def test_elm_driver():
    print("-- драйвер против имитатора ELM")
    p = P.Project.open(A2L, V2M)
    fm = udk.FuelModel.from_project(p)
    car = fakecar.FakeCar(fm)
    clk = fakecar.VirtualClock(10.0)
    fe = fakecar.FakeElm(car, clk, count_suffix=False)
    e = E.Elm327(fe, clock=clk)
    msgs = []
    e.init("0", msgs.append)
    check("ELM327 v1.5" in e.version and e.protocol == "A5",
          "инициализация: %s, протокол %s" % (e.version, e.protocol))
    sup = e.supported_pids()
    check({0x0C, 0x10, 0x11, 0x06, 0x07, 0x05, 0x03} <= sup,
          "поддержанные PID прочитаны")
    r = e.query(1, 0x0C, 2)
    check(r is not None and not e.count_suffix,
          "клон без числа ответов: «?» понят, дальше шлём без него")
    check(e.query(1, 0x0A, 1) is None and e.stats.no_data >= 1,
          "неподдержанный PID -- NO DATA, без падения")

    class Dead(E.Transport):
        def write(self, d):
            pass

        def read(self, t):
            time.sleep(t)
            return b""
    try:
        E.Elm327(Dead()).command("ATZ", 0.2)
        check(False, "молчащий адаптер обязан дать ошибку")
    except E.ElmError as exc:
        check("не ответил" in str(exc), "молчащий адаптер: «%s»" % exc)


def test_isp2():
    print("-- УДК: Innovate ISP2")
    pk = W.isp2_packet(0.873) + W.isp2_packet(None, func=4) + \
        W.isp2_packet(1.25, aux=2) + W.isp2_packet(None, func=6)
    p = W.Isp2Parser()
    out = []
    for b in b"\x13\x37garbage" + pk:         # мусор и по одному байту
        out += p.feed(bytes([b]))
    check(len(out) == 4, "четыре пакета из побайтного потока с мусором: %d"
          % len(out))
    check(abs(out[0].lam - 0.873) < 0.0011 and out[0].status == W.OK,
          "лямбда 0.873 -> %.3f" % (out[0].lam or 0))
    check(abs(out[0].raw - 0.873 * 14.7) < 0.02,
          "AFR через множитель из пакета: %.2f" % out[0].raw)
    check(out[1].lam is None and out[1].status == W.WARMUP,
          "прогрев -- лямбды нет, состояние «прогрев»")
    check(abs(out[2].lam - 1.25) < 0.0011,
          "пакет со вспомогательными каналами разобран")
    check(out[3].lam is None and out[3].status == W.ERROR, "ошибка датчика")
    half = W.isp2_packet(0.95)
    p = W.Isp2Parser()
    check(p.feed(half[:3]) == [] and len(p.feed(half[3:])) == 1,
          "пакет, разрезанный посередине, собирается")


def test_ascii():
    print("-- УДК: текст")
    p = W.AsciiParser()
    out = p.feed(b"14.70\r\n1,000\r\nAFR: 13.23\r\nhello\r\n22.5")
    check(len(out) == 3, "три строки с числами, мусорная пропущена, "
          "незаконченная ждёт")
    check(abs(out[0].lam - 1.0) < 1e-9 and abs(out[1].lam - 1.0) < 1e-9
          and abs(out[2].lam - 0.9) < 1e-9,
          "AFR и лямбда различены по величине, десятичная запятая")
    out = p.feed(b"\r\n")
    check(len(out) == 1 and abs(out[0].lam - 22.5 / 14.7) < 1e-9,
          "строка дописалась -- разобрана")
    p = W.AsciiParser(kind="afr")
    check(abs(p.feed(b"14.7\n")[0].lam - 1.0) < 1e-9, "явно AFR")
    check(W.AsciiParser().feed(b"99\n")[0].lam is None,
          "AFR 99 -- не лямбда, а ошибка")


def test_schedule():
    print("-- план опроса")
    order = livelog.schedule(obd.PLAN_01, None)
    cnt = {k[1]: order.count(k) for k in set(order)}
    check(cnt[0x0C] == cnt[0x10] == cnt[0x11] == 40 and cnt[0x05] == 2,
          "обороты, расход, дроссель -- каждый круг, ОЖ -- раз в 20: %s"
          % {"%02X" % k: v for k, v in sorted(cnt.items())})
    order = livelog.schedule(obd.PLAN_01, {0x0C, 0x10, 0x11})
    check({k[1] for k in order} == {0x0C, 0x10, 0x11},
          "неподдержанные PID не спрашиваются")
    order = livelog.schedule(obd.PLAN_22, None, set())
    check(all(m == 1 for m, _ in order),
          "режим 22 не ответил -- его величины не спрашиваются")
    check(livelog.plan_for({0x1235}) is obd.PLAN_22
          and livelog.plan_for(set()) is obd.PLAN_01,
          "есть АЦП ДМРВ -- план с ним, нет -- обычный")


def test_end_to_end():
    print("-- логгер -> файл -> расчёт")
    p = P.Project.open(A2L, V2M)
    fm = udk.FuelModel.from_project(p)
    car = fakecar.FakeCar(fm)
    clk = fakecar.VirtualClock(1000.0)
    fe = fakecar.FakeElm(car, clk)
    e = E.Elm327(fe, clock=clk)
    e.init("5")
    sup = e.supported_pids()
    tmp = tempfile.mkdtemp(prefix="livelog")
    try:
        path = os.path.join(tmp, "лог", "поездка.csv")
        t0 = clk.t
        L = livelog.LiveLogger(e, fakecar.FakeWideband(car, clk, lag=0.3),
                               W.Isp2Parser(), path=path, clock=clk,
                               supported=sup)
        # модель начинает поездку с t=0: подождать, пока круг начнётся
        L.run_for(car.period - (t0 % car.period) + car.period)
        L.rec.close()
        r = L.rates(window=1e9)
        check(1.8 < r[obd.PIDS[0x0C].name] < 3.0
              and 10 < r[livelog.LAMBDA_NAME] < 13,
              "частота: обороты %.1f Гц, лямбда %.1f Гц"
              % (r[obd.PIDS[0x0C].name], r[livelog.LAMBDA_NAME]))
        check(L.wb.bad.get(W.WARMUP, 0) > 0
              and livelog.LAMBDA_NAME in L.rec.channels,
              "прогрев датчика в лог не попал: пропущено %d пакетов"
              % L.wb.bad.get(W.WARMUP, 0))
        log = logio.read_log(path)
        check(log.fmt == "long" and set(log.channels) == set(L.rec.channels),
              "файл читается тем же разбором, что и Car Scanner")
        ch = log.channels[obd.PIDS[0x0C].name]
        check(all(ch.t[i] <= ch.t[i + 1] for i in range(len(ch.t) - 1)),
              "время в файле не убывает")
        roles = udk.detect_roles(log, None)
        check(roles["lam"] == ("ecu", livelog.LAMBDA_NAME)
              and roles["stft"] and roles["ect"],
              "роли распознаны в своём же логе, УДК в нём же")
        res = udk.analyze(fm, log, None, roles, udk.Settings(target="hfm"))
        err = udksim.err_lean_top
        devs = []
        for n in res.nodes:
            q = fm.hfm_flow(int(n.x))
            if n.status != "по данным" or q < 60:
                continue
            qt = q
            for _ in range(50):
                qt = q * err(qt)
            devs.append(abs(n.new - err(qt)))
        check(len(devs) >= 6 and max(devs) < 0.025,
              "по логу логгера ошибка ДМРВ найдена: %d узлов, расхождение "
              "до %.1f %%" % (len(devs), max(devs) * 100 if devs else 99))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _node_devs(fm, res, err):
    devs = []
    for n in res.nodes:
        q = fm.hfm_flow(int(n.x))
        if n.status != "по данным" or q < 60:
            continue
        qt = q
        for _ in range(50):
            qt = q * err(qt)
        devs.append(abs(n.new - err(qt)))
    return devs


def test_mode22():
    print("-- режим 22: сырой АЦП ДМРВ и коробка, отвечающая первой")
    p = P.Project.open(A2L, V2M)
    fm = udk.FuelModel.from_project(p)

    # поправка пульсаций, как у KFPUSU: до +13 % на широком дросселе.
    # Расход в OBD уже умножен на неё, а АЦП -- нет
    def pk(rpm, tps):
        return 1.0 + 0.13 * min(1.0, tps / 100.0) * min(1.0, rpm / 6000.0)

    import udksim as US
    tr = US.run(fm, fm.hfm_raw, ltft=0.0, pk=pk)

    class Car(fakecar.FakeCar):
        def __init__(self):
            self.truth = tr
            self.dt = tr[1].t - tr[0].t
            self.period = len(tr) * self.dt
    car = Car()
    results = {}
    for use22 in (False, True):
        clk = fakecar.VirtualClock(0.0)
        fe = fakecar.FakeElm(car, clk, mode22=use22, tcm=True)
        e = E.Elm327(fe, clock=clk)
        msgs = []
        e.init("0", msgs.append)
        if use22:
            check(e.ecus == {0x10, 0x18} and e.ecu == 0x10
                  and not e.count_suffix,
                  "ответили двигатель и коробка: беру 10, число ответов не "
                  "шлю (%s)" % "; ".join(msgs[-1:]))
        sup = e.supported_pids()
        dids = livelog.probe_dids(e)
        check((dids == set(obd.DIDS)) == use22,
              "режим 22 %s: %s" % ("есть" if use22 else "нет",
                                   " ".join("%04X" % d for d in sorted(dids))))
        L = livelog.LiveLogger(e, fakecar.FakeWideband(car, clk), W.Isp2Parser(),
                               clock=clk, supported=sup, dids=dids)
        L.run_for(car.period - 1.0)
        log = L.rec.snapshot()
        rpm = log.channels[obd.PIDS[0x0C].name].v
        check(1234.0 not in rpm, "обороты коробки (1234) в лог не попали")
        roles = udk.detect_roles(log, None)
        if use22:
            check(roles["adc"] == ("ecu", obd.DIDS[0x1235].name)
                  and roles["maf"] == ("ecu", obd.DIDS[0x1285].name),
                  "ключ -- АЦП, расход -- из режима 22: %s, %s"
                  % (roles["adc"], roles["maf"]))
        res = udk.analyze(fm, log, None, roles, udk.Settings(target="hfm"))
        results[use22] = _node_devs(fm, res, US.err_lean_top)
    d01, d22 = max(results[False]), max(results[True])
    check(d22 < 0.015 and d22 < d01,
          "с АЦП ключ точнее: расхождение %.1f %% против %.1f %% по расходу "
          "с пульсациями" % (d22 * 100, d01 * 100))


def test_threads():
    print("-- потоками, по настоящим часам")
    p = P.Project.open(A2L, V2M)
    fm = udk.FuelModel.from_project(p)
    car = fakecar.FakeCar(fm)
    fe = fakecar.FakeElm(car, time.monotonic, latency=0.02, realtime=True)
    e = E.Elm327(fe)
    e.init("0")
    L = livelog.LiveLogger(
        e, fakecar.FakeWideband(car, time.monotonic, realtime=True),
        W.Isp2Parser(), supported=e.supported_pids())
    L.start()
    time.sleep(3.0)
    L.stop()
    check(not L.running, "потоки остановились")
    check(len(L.rec.channels.get(obd.PIDS[0x0C].name, [])) > 10
          and len(L.rec.channels.get(livelog.LAMBDA_NAME, [])) > 5,
          "за 3 с записано: обороты %d, лямбда %d" % (
              len(L.rec.channels.get(obd.PIDS[0x0C].name, [])),
              len(L.rec.channels.get(livelog.LAMBDA_NAME, []))))
    check(not L.error, "ошибок нет: %s" % (L.error or "-"))


def main():
    test_elm_grammar()
    test_elm_driver()
    test_isp2()
    test_ascii()
    test_schedule()
    test_end_to_end()
    test_mode22()
    test_threads()
    print()
    if fails:
        print("ПРОВАЛЕНО: %d" % len(fails))
        return 1
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
