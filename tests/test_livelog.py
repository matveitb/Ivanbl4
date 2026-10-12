#!/usr/bin/env python3
"""
Проверка логгера: ELM (с УДК и STFT в PID 14), ШДК, запись, сквозной прогон до поправки.

Машины в проверке нет, поэтому собеседники -- имитаторы из
editor/core/fakecar.py: ELM327 v1.5, который отвечает текстом как
настоящий (эхо, «SEARCHING...», «NO DATA», «?»), и ШДК, который шлёт
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
    def row(*bs):
        """Строка ответа с верной суммой в конце."""
        return " ".join("%02X" % x for x in list(bs) + [sum(bs) & 0xFF])

    rows = E.data_bytes(E.clean_lines(row(0x84, 0xF1, 0x17, 0x41, 0x0C, 0x1A,
                                          0xF8) + "\r\r>"))
    check(E.find_reply(rows, 1, 0x0C, 2) == [0x1A, 0xF8],
          "с заголовками KWP: «84 F1 17 41 0C 1A F8 сумма»")
    bad = "84 F1 17 41 0C 1A F8 00\r\r>"
    check(E.find_reply(E.data_bytes(E.clean_lines(bad)), 1, 0x0C, 2) is None,
          "неверная сумма -- строка отброшена")
    rows = E.data_bytes(E.clean_lines(
        row(0x84, 0xF1, 0x18, 0x41, 0x0C, 0x04, 0xD2) + "\r" +
        row(0x84, 0xF1, 0x17, 0x41, 0x0C, 0x1A, 0xF8) + "\r\r>"))
    check(E.find_reply(rows, 1, 0x0C, 2, src=0x17) == [0x1A, 0xF8],
          "ответили коробка (18) и двигатель (17) -- взят двигатель")
    rows = E.data_bytes(E.clean_lines(row(0x48, 0x6B, 0x17, 0x41, 0x0C, 0x1A,
                                          0xF8) + "\r\r>"))
    check(E.find_reply(rows, 1, 0x0C, 2, src=0x17) == [0x1A, 0xF8],
          "заголовок ISO 9141: «48 6B 17 ...»")
    rows = E.data_bytes(E.clean_lines(row(0x83, 0xF1, 0x17, 0x41, 0x0C, 0x1A,
                                          0xF8) + "\r\r>"))
    check(E.find_reply(rows, 1, 0x0C, 2) == [0x1A, 0xF8],
          "длина в заголовке не сходится -- данные берутся по факту")
    rows = E.data_bytes(E.clean_lines(row(0x48, 0x6B, 0x17, 0x62, 0x12, 0x35,
                                          0x01, 0xE7) + "\r\r>"))
    check(E.find_reply(rows, 0x22, [0x12, 0x35], 2) == [0x01, 0xE7],
          "режим 22: «62 12 35 01 E7» -> АЦП 0x1E7")
    rows = E.data_bytes(E.clean_lines(row(0x83, 0xF1, 0x17, 0x63, 0x00, 0x10)
                                      + "\r\r>"))
    check(E.find_reply(rows, 0x23, [], 2) == [0x00, 0x10],
          "чтение памяти: «63 00 10» -> 0x1000, λ 1.000")
    for txt, want in (("BUS INIT: ...ERROR\r\r>", "BUS INIT: ERROR"),
                      ("BUS INIT: ERROR\r\r>", "BUS INIT: ERROR"),
                      ("BUS INIT: ...\rERROR\r\r>", "BUS INIT: ERROR"),
                      ("BUS INIT: ...\rUNABLE TO CONNECT\r\r>",
                       "BUS INIT: ERROR"),
                      ("SEARCHING...\rUNABLE TO CONNECT\r\r>",
                       "UNABLE TO CONNECT"),
                      ("LV RESET\r\r>", "LV RESET")):
        err = E.is_error(E.clean_lines(txt))
        check(err == want and E.is_fatal(err),
              "«%s» -> «%s», нужна новая инициализация"
              % (txt.split("\r\r")[0].replace("\r", " | "), err))
    check(E.clean_lines("BUS INIT: OK\r410C1AF8\r\r>") == ["410C1AF8"],
          "«BUS INIT: OK» быстрой инициализации вычищен")
    check(not E.is_fatal("NO DATA") and not E.is_fatal("ERROR"),
          "NO DATA -- не повод переподключаться")
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
    for pid in (0x03, 0x05, 0x06, 0x07, 0x0C, 0x11):
        v = {0x03: 2, 0x05: 88, 0x06: -3.125, 0x07: 4.6875, 0x0C: 3000.25,
             0x11: 100 * 51 / 255}[pid]
        got = obd.PIDS[pid].decode(obd.encode(pid, v))
        check(abs(got - v) < 1e-9, "PID %02X туда и обратно: %g" % (pid, got))
    # PID 10: блок шлёт floor(ml/9)*25 -- шаг 0.25 г/с, всегда вниз;
    # разбор возвращает середину ступеньки, ошибка в среднем ноль
    errs = []
    for k in range(2000):
        gs = 2.0 + k * 0.0371
        errs.append(obd.PIDS[0x10].decode(obd.encode(0x10, gs)) - gs)
    mean = sum(errs) / len(errs)
    # остаётся только округление самого блока до целых 0.1 кг/ч (-0.014)
    check(abs(mean) < 0.02 and max(abs(e) for e in errs) <= 0.14,
          "PID 10: средняя ошибка %+.3f г/с, наибольшая %.3f -- ступенька "
          "блока учтена" % (mean, max(abs(e) for e in errs)))
    check(obd.PIDS[0x10].decode(bytes([0xFF, 0xFF])) is None,
          "PID 10 = FFFF -- упор, значения нет")
    # PID 14: напряжение УДК и STFT одним ответом
    p14 = obd.PIDS[0x14]
    body = obd.encode(0x14, (0.725, -3.125))
    (name, unit, dec), = p14.extra
    check(abs(p14.decode(body) - 0.725) < 1e-9 and abs(dec(body) + 3.125) < 1e-9
          and name == obd.PIDS[0x06].name,
          "PID 14: УДК %.3f В и STFT %+.3f %% -- в тот же канал, что PID 06"
          % (p14.decode(body), dec(body)))
    check(dec(obd.encode(0x14, 0.45)) is None,
          "PID 14 со вторым байтом FF -- STFT нет, а не -100 %")


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
    check("ELM327 v1.5" in e.version and e.protocol == "A3",
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
    print("-- ШДК: Innovate ISP2")
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
    # настоящие пакеты из открытых разборщиков (RomRaider, Evo-live-map)
    out = W.Isp2Parser().feed(bytes.fromhex("B28243130A60B2824713015"
                                            "1B2874313036B" + "0000" * 5))
    check(len(out) == 3 and abs(out[0].lam - 1.876) < 1e-9
          and out[1].status == W.O2 and abs(out[1].raw - 20.9) < 1e-9
          and abs(out[2].lam - 0.991) < 1e-9,
          "настоящие пакеты: λ 1.876, O2 20.9 %%, λ 0.991 -- %s" % [
              (r.status, r.lam, r.raw) for r in out])
    two = W.isp2_packet(0.85, sensors=2)
    out = W.Isp2Parser().feed(two + two)
    check(len(out) == 2, "LM-2 / цепочка из двух датчиков: по показанию на "
          "пакет, а не вперемешку")
    check(W.Isp2Parser().feed(bytes.fromhex("B2824231000024"))[0].status
          == W.ERROR, "множитель AFR 4.9 -- слово битое, не «λ 0.536»")
    out = W.Isp2Parser().feed(W.isp2_packet(None, func=4, L=731))
    check(out[0].status == W.WARMUP and abs(out[0].raw - 73.1) < 1e-9,
          "прогрев: 73.1 %% рабочей температуры")


def test_ascii():
    print("-- ШДК: текст")
    p = W.AsciiParser()
    out = p.feed(b"14.70\r\n1,000\r\n13.23\r\nhello\r\n22.5")
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
    check(W.AsciiParser().feed(b"Board: VBatt 13.8 Temp 35 deg C\r\n") == [],
          "строка со словами в начале -- не показание (а то 13.8 В "
          "записалось бы как AFR)")
    out = W.AsciiParser().feed(b"1.000\tReady\tNo-errors\r"
                               b"0.850\tWarming\tNo-errors\r"
                               b"0.950\tReady\tHeater open\r")
    check([r.status for r in out] == [W.OK, W.WARMUP, W.ERROR]
          and out[0].lam == 1.0,
          "AEM с полями состояния: годно, прогрев, ошибка")
    check(W.AsciiParser().feed(W.isp2_packet(1.0) * 40 + b"\r\n") == [],
          "двоичный поток при выбранном тексте -- ни одного показания")


def test_plx_zt2():
    print("-- ШДК: PLX и Zeitronix")
    p = W.PlxParser()
    out = p.feed(W.plx_packet(0.95) + W.plx_packet(1.10, instance=1)
                 + W.plx_packet(0.90))
    lams = [r.lam for r in out]
    check(len(lams) == 2 and abs(lams[0] - 0.95) < 0.003
          and abs(lams[1] - 0.90) < 0.003,
          "PLX: второй модуль (номер 1) не подмешан: %s"
          % [round(x, 3) for x in lams])
    broken = bytearray(W.plx_packet(0.95))
    del broken[3]
    out = W.PlxParser().feed(bytes(broken) + W.plx_packet(1.05))
    check(len(out) == 1 and abs(out[0].lam - 1.05) < 0.003,
          "PLX: пакет с потерянным байтом выброшен целиком, следующий цел")
    out = W.PlxParser().feed(W.plx_packet(0.95)[:-1] + W.plx_packet(1.0))
    check(len(out) == 1 and abs(out[0].lam - 1.0) < 0.003,
          "PLX: потерян конец пакета -- начало берётся от последнего 0x80")
    out = W.ZeitronixParser().feed(b"\x55" + W.zt2_packet(0.92)
                                   + W.zt2_packet(1.0)[:7])
    check(len(out) == 1 and abs(out[0].lam - 0.92) < 0.004,
          "Zeitronix: мусор перед пакетом, недописанный хвост ждёт")


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
    order = livelog.schedule(obd.PLAN_01, None)
    check(order.count((1, 0x14)) == order.count((1, 0x0C))
          and (1, 0x06) not in order,
          "без ШДК УДК со STFT (PID 14) -- каждый круг, PID 06 не нужен")
    order = livelog.schedule(obd.PLAN_01, {0x0C, 0x10, 0x11, 0x06})
    check(order.count((1, 0x06)) == order.count((1, 0x0C))
          and (1, 0x14) not in order,
          "нет PID 14 -- STFT по PID 06, так же часто")
    order = livelog.schedule(livelog.plan_for(set(), None, wb=True), None)
    check(2 * order.count((1, 0x14)) == order.count((1, 0x0C)),
          "с ШДК УДК со STFT -- через круг: чаще обороты и дроссель")


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
              "роли распознаны в своём же логе, ШДК в нём же")
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


def test_ports():
    print("-- порты: какой из них адаптер")
    bt = r"BTHENUM\{00001101-0000-1000-8000-00805F9B34FB}_LOCALMFG&"
    out_hw = bt + r"0002\7&2B1A8F16&0&001DA5686E0D_C00000000"
    in_hw = bt + r"0000\7&2B1A8F16&0&000000000000_00000000"
    desc = "Стандартный последовательный порт по соединению Bluetooth (%s)"
    names = {"001DA5686E0D": "OBDII"}
    ps = [E.classify_port("COM6", desc % "COM6", in_hw, names.get),
          E.classify_port("COM1", "Последовательный порт (COM1)",
                          r"ACPI\PNP0501\1", names.get),
          E.classify_port("COM5", desc % "COM5", out_hw, names.get),
          E.classify_port("COM3", "USB-SERIAL CH340 (COM3)",
                          "USB VID:PID=1A86:7523 SER= LOCATION=1-2",
                          names.get)]
    lab = {p.device: p.label for p in ps}
    check(lab["COM5"] == "COM5 · Bluetooth «OBDII» · исходящий"
          and "входящий" in lab["COM6"] and lab["COM3"] == "COM3 · USB-SERIAL CH340",
          "подписи: %s" % " | ".join(lab[d] for d in ("COM5", "COM6", "COM3")))
    order = [p.device for p in sorted(ps, key=lambda p: p.rank)]
    check(order[0] == "COM5" and order[-1] == "COM6",
          "Bluetooth-адаптер первым, входящий порт последним: %s"
          % " ".join(order))

    p = P.Project.open(A2L, V2M)
    car = fakecar.FakeCar(udk.FuelModel.from_project(p))
    tried = []

    def opener(dev):
        tried.append(dev)
        if dev == "COM5":
            return fakecar.FakeElm(car, fakecar.VirtualClock(0.0))
        raise E.ElmError("на %s адаптер не ответил ни на одной скорости" % dev)
    msgs = []
    found = E.find_elm(ps, opener=opener, log=msgs.append)
    check(found == "COM5" and tried[0] == "COM5" and "COM6" not in tried,
          "«Найти адаптер»: ELM327 на COM5 найден с первой попытки, входящий "
          "порт не трогали (%s)" % msgs[-1])
    tried.clear()
    found = E.find_elm([q for q in ps if q.device != "COM5"], opener=opener)
    check(found is None and tried == ["COM3", "COM1"],
          "без адаптера -- None, перебраны все, кроме входящего: %s"
          % " ".join(tried))


def test_narrowband():
    print("-- без ШДК: УДК и STFT из PID 14 -> расчёт по коррекциям")
    p = P.Project.open(A2L, V2M)
    fm = udk.FuelModel.from_project(p)
    car = fakecar.FakeCar(fm, by_code=True, nb_ctrl=True, ltft=3.0)
    clk = fakecar.VirtualClock(0.0)
    fe = fakecar.FakeElm(car, clk)
    e = E.Elm327(fe, clock=clk)
    e.init("3")
    sup = e.supported_pids()
    dids = livelog.probe_dids(e)
    L = livelog.LiveLogger(e, clock=clk, supported=sup, dids=dids)
    check(L.wb is None and (1, 0x14) in L.ecu.order
          and (1, 0x06) not in L.ecu.order,
          "ШДК нет -- STFT берётся из PID 14 вместе с УДК")
    L.run_for(car.period - 1.0)
    log = L.rec.snapshot()
    o2 = log.channels.get(obd.PIDS[0x14].name)
    st = log.channels.get(obd.PIDS[0x06].name)
    r = L.rates(window=1e9)
    check(o2 is not None and st is not None
          and len(o2.v) == len(st.v) and min(o2.v) < 0.2 < 0.7 < max(o2.v),
          "в логе УДК (%.2f…%.2f В) и STFT, по %.1f в секунду"
          % (min(o2.v) if o2 else 0, max(o2.v) if o2 else 0,
             r.get(obd.PIDS[0x06].name, 0)))
    roles = udk.detect_roles(log, None)
    check(roles["lam"] is None and roles["o2v"] == ("ecu", o2.name)
          and roles["closed"] == ("ecu", obd.DIDS[0x1216].name),
          "роли: ШДК нет, УДК и флаг регулятора (DID 1216) найдены")
    res = udk.analyze(fm, log, None, roles, udk.Settings(target="hfm"))
    devs = _node_devs(fm, res, udksim.err_lean_top)
    check(res.narrow and len(devs) >= 6 and max(devs) < 0.025,
          "по коррекциям ошибка ДМРВ найдена: %d узлов, расхождение до "
          "%.1f %%" % (len(devs), max(devs) * 100 if devs else 99))


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
            check(e.ecus == {0x17, 0x18} and e.ecu == 0x17
                  and not e.count_suffix,
                  "ответили двигатель (17, как в прошивке) и коробка: беру "
                  "17, число ответов не шлю (%s)" % "; ".join(msgs[-1:]))
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


def test_sessions_and_faults():
    print("-- две двери блока, сбои шины и адаптера")
    p = P.Project.open(A2L, V2M)
    fm = udk.FuelModel.from_project(p)
    car = fakecar.FakeCar(fm)

    # автопоиск попал в KWP, чтение памяти работает: АЦП и заданная лямбда
    clk = fakecar.VirtualClock(0.0)
    e = E.Elm327(fakecar.FakeElm(car, clk, auto_iso=False, mem23=True),
                 clock=clk)
    e.init("0")
    msgs = []
    sup, dids, mems = livelog.prepare(e, msgs.append)
    check(e.protocol == "A5" and not dids and mems == set(obd.MEMS),
          "KWP: режима 22 нет (7F 22 12), память читается: %s; %s"
          % (" ".join("%06X" % a for a in sorted(mems)), "; ".join(msgs)))
    check("отказ блока: 7F 22 12" in e.stats.last_error or e.stats.errors,
          "отказ «7F 22 12» распознан как отказ, а не мусор")
    L = livelog.LiveLogger(e, fakecar.FakeWideband(car, clk), W.Isp2Parser(),
                           clock=clk, supported=sup, dids=dids, mems=mems)
    L.run_for(car.period - 5)
    log = L.rec.snapshot()
    roles = udk.detect_roles(log, None)
    check(roles["lcmd"] == ("ecu", obd.MEMS[0x381818].name)
          and roles["adc"] == ("ecu", obd.MEMS[0x38103C].name),
          "в логе заданная лямбда и АЦП из памяти")
    res = udk.analyze(fm, log, None, roles, udk.Settings(target="hfm"))
    check(any("из блока" in n for n in res.notes),
          "расчёт берёт заданную лямбду из блока, а не из модели")
    check(len(_node_devs(fm, res, udksim.err_lean_top)) >= 6
          and max(_node_devs(fm, res, udksim.err_lean_top)) < 0.02,
          "и находит ошибку ДМРВ: до %.1f %%"
          % (max(_node_devs(fm, res, udksim.err_lean_top)) * 100))

    # автопоиск попал в KWP, памяти нет -- сам переходит на ISO 9141
    clk = fakecar.VirtualClock(0.0)
    e = E.Elm327(fakecar.FakeElm(car, clk, auto_iso=False, mem23=False),
                 clock=clk)
    e.init("0")
    msgs = []
    sup, dids, mems = livelog.prepare(e, msgs.append)
    check(e.protocol == "3" and dids == set(obd.DIDS) and not mems,
          "KWP без памяти -> переподключился по ISO 9141, режим 22 есть "
          "(%s)" % "; ".join(msgs[:1]))

    # человек сам выбрал KWP -- не трогаем
    clk = fakecar.VirtualClock(0.0)
    e = E.Elm327(fakecar.FakeElm(car, clk, protocol="kwp"), clock=clk)
    e.init("5")
    sup, dids, mems = livelog.prepare(e)
    check(e.protocol == "5" and not dids and not mems,
          "выбран KWP руками -- остаёмся на нём, пишем по PID")

    # шина не поднялась (зажигание выключено)
    clk = fakecar.VirtualClock(0.0)
    e = E.Elm327(fakecar.FakeElm(car, clk, fail_inits=1), clock=clk)
    try:
        e.init("0")
        check(False, "без шины init обязан отказаться")
    except E.ElmError as exc:
        check("UNABLE TO CONNECT" in str(exc),
              "нет шины -- понятная ошибка: %s" % exc)
    e.init("0")
    check(e.ecu == 0x17, "со второй попытки связь есть")

    # адаптер сбросился посреди записи -- логгер переподключился сам
    clk = fakecar.VirtualClock(0.0)
    fe = fakecar.FakeElm(car, clk, lv_reset_at=60)
    e = E.Elm327(fe, clock=clk)
    e.init("0")
    sup, dids, mems = livelog.prepare(e)
    L = livelog.LiveLogger(e, None, None, clock=clk, supported=sup,
                           dids=dids, mems=mems)
    L.run_for(60)
    rpm = L.rec.channels[obd.PIDS[0x0C].name]
    gap = max(b - a for a, b in zip(rpm.t, rpm.t[1:]))
    check(L.reconnects == 1 and rpm.t[-1] > 55 and gap < 10,
          "LV RESET: переподключился %d раз, запись шла дальше (дыра %.1f с)"
          % (L.reconnects, gap))
    check(e.headers_ok if hasattr(e, "headers_ok") else e.ecu == 0x17,
          "после сброса заголовки снова включены, адрес двигателя тот же")

    # оборвался Bluetooth: старый порт мёртв, открываем новый
    clk = fakecar.VirtualClock(0.0)
    ports = []

    def reopen():
        fe = fakecar.FakeElm(car, clk)
        ports.append(fe)
        return fe
    e = E.Elm327(reopen(), clock=clk, reopen=reopen)
    e.init("0")
    sup, dids, mems = livelog.prepare(e)
    L = livelog.LiveLogger(e, None, None, clock=clk, supported=sup,
                           dids=dids, mems=mems)
    L.run_for(20)
    ports[0].close()
    L.run_for(40)
    rpm = L.rec.channels[obd.PIDS[0x0C].name]
    check(len(ports) == 2 and L.reconnects == 1 and rpm.t[-1] > 55,
          "обрыв порта: открыт новый, запись продолжилась (%d порта)"
          % len(ports))


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
    test_plx_zt2()
    test_schedule()
    test_ports()
    test_narrowband()
    test_end_to_end()
    test_mode22()
    test_sessions_and_faults()
    test_threads()
    print()
    if fails:
        print("ПРОВАЛЕНО: %d" % len(fails))
        return 1
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
