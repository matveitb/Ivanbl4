#!/usr/bin/env python3
"""
Проверка лаунч-контроля (tools/launch.py, docs/40).

Три слоя, и каждый ловит своё:

1. Опора в коде. То, на чём стоит врезка, проверяется по стоковому образу:
   что 0xFD16.5 действительно ставит отключение подачи на всех цилиндрах,
   что 0xFD6E.5 -- это отсечка по перегреву, что 0xB37C.0 и 0xB304.0 --
   неисправности датчиков скорости и дросселя, и т. д. Поменяется
   толкование -- упадёт здесь, а не на машине.

2. Байты. Подпрограмма собрана руками, поэтому её разбирает дизассемблер,
   порождённый из c166.sinc, и текст сверяется построчно.

3. Поведение. Маленький исполнитель C166 -- ровно на те команды, что есть
   в подпрограмме, на любой другой он падает -- прогоняет путь от 0x83C116
   через вызов до развилки 0x83C11C / 0x83C120 и сравнивает с моделью на
   Python на тысячах входов. Отдельно: когда условия лаунча не выполнены,
   результат совпадает со стоком бит в бит.
"""

import glob
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import bosch_csum  # noqa: E402
import c166dis     # noqa: E402
import fwlib       # noqa: E402
import launch      # noqa: E402

fails = []


def check(cond, msg):
    print(("OK   " if cond else "ОШИБКА ") + msg)
    if not cond:
        fails.append(msg)


def fw(name):
    return open(os.path.join(ROOT, "firmware", name), "rb").read()


def text(data, start, end):
    dis = c166dis.Disassembler()
    return [(i.addr, " ".join(i.text().split()))
            for i in c166dis.disassemble(data, start, end, base=0x800000, dis=dis)]


def body(data, start, end):
    return " | ".join(t for _a, t in text(data, start, end))


# --------------------------------------------------------------------------
# исполнитель

class Unsupported(Exception):
    pass


class Cpu:
    """
    Только команды подпрограммы и места врезки. Память: 0x4000..0x7FFF --
    страница DPP1 (файл 0x14000), остальное -- словарь «ОЖ/ОЗУ». GPR --
    отдельный массив; битовые адреса 0x00..0x7F -> слово 0xFD00 + 2*n,
    0xF0..0xFF -> r0..r15.
    """

    def __init__(self, image, ram):
        self.img = image
        self.ram = dict(ram)
        self.r = [0] * 16
        self.C = self.Z = self.N = self.V = False
        self.stack = []
        self.touched = set()

    # память
    def rd8(self, a):
        if 0x4000 <= a < 0x8000:
            return self.img[launch.DPP1_FILE + a - 0x4000]
        if a not in self.ram:
            raise Unsupported("чтение незаданной ячейки 0x%04X" % a)
        return self.ram[a]

    def rd16(self, a):
        assert a % 2 == 0, "невыровненное слово 0x%04X" % a
        return self.rd8(a) | (self.rd8(a + 1) << 8)

    def bit(self, bitoff, b):
        if bitoff < 0x80:
            return (self.rd16(0xFD00 + 2 * bitoff) >> b) & 1
        if bitoff >= 0xF0:
            return (self.r[bitoff - 0xF0] >> b) & 1
        raise Unsupported("битовый адрес 0x%02X" % bitoff)

    # регистры
    def getb(self, b):
        w = self.r[b >> 1]
        return (w >> 8) & 0xFF if b & 1 else w & 0xFF

    def setb(self, b, v):
        w = self.r[b >> 1]
        self.r[b >> 1] = (w & 0x00FF) | (v << 8) if b & 1 else (w & 0xFF00) | v
        self.touched.add(b >> 1)

    def setw(self, n, v):
        self.r[n] = v & 0xFFFF
        self.touched.add(n)

    def flags_mov(self, v, bits):
        self.Z = v == 0
        self.N = bool(v >> (bits - 1))

    def flags_sub(self, a, b, bits):
        m = (1 << bits) - 1
        res = (a - b) & m
        self.C = a < b
        self.Z = res == 0
        self.N = bool(res >> (bits - 1))
        sa = a - (1 << bits) if a >> (bits - 1) else a
        sb = b - (1 << bits) if b >> (bits - 1) else b
        self.V = not (-(1 << (bits - 1)) <= sa - sb < (1 << (bits - 1)))
        return res

    def cc(self, c):
        C, Z, N, V = self.C, self.Z, self.N, self.V
        table = {0x0: True, 0x2: Z, 0x3: not Z, 0x8: C, 0x9: not C,
                 0xE: not C and not Z, 0xF: C or Z, 0x6: N, 0x7: not N,
                 0xA: not Z and (N == V), 0xB: Z or (N != V),
                 0xC: N != V, 0xD: N == V}
        if c not in table:
            raise Unsupported("условие %X" % c)
        return table[c]

    def step(self, pc):
        f = pc - launch.BASE
        b = self.img[f:f + 4]
        op = b[0]
        s8 = lambda x: x - 256 if x > 127 else x            # noqa: E731
        if op in (0x8A, 0x9A):                              # jb / jnb bitoff.b
            v = self.bit(b[1], b[3] >> 4)
            take = v if op == 0x8A else not v
            return pc + 4 + (2 * s8(b[2]) if take else 0)
        if op == 0xF2 and b[1] >> 4 == 0xF:                 # mov Rw, mem
            v = self.rd16(b[2] | b[3] << 8)
            self.setw(b[1] & 0xF, v)
            self.flags_mov(v, 16)
            return pc + 4
        if op == 0xF3 and b[1] >> 4 == 0xF:                 # movb Rb, mem
            v = self.rd8(b[2] | b[3] << 8)
            self.setb(b[1] & 0xF, v)
            self.flags_mov(v, 8)
            return pc + 4
        if op == 0x43 and b[1] >> 4 == 0xF:                 # cmpb Rb, mem
            self.flags_sub(self.getb(b[1] & 0xF), self.rd8(b[2] | b[3] << 8), 8)
            return pc + 4
        if op == 0x42 and b[1] >> 4 == 0xF:                 # cmp Rw, mem
            self.flags_sub(self.r[b[1] & 0xF], self.rd16(b[2] | b[3] << 8), 16)
            return pc + 4
        if op == 0x22 and b[1] >> 4 == 0xF:                 # sub Rw, mem
            n = b[1] & 0xF
            self.setw(n, self.flags_sub(self.r[n], self.rd16(b[2] | b[3] << 8), 16))
            return pc + 4
        if op & 0x0F == 0x0D:                               # jmpr cc, rel
            return pc + 2 + (2 * s8(b[1]) if self.cc(op >> 4) else 0)
        if op == 0xE0:                                      # mov Rw, #data4
            v = b[1] >> 4
            self.setw(b[1] & 0xF, v)
            self.flags_mov(v, 16)
            return pc + 2
        if op == 0xDA:                                      # calls seg, caddr
            self.stack.append(pc + 4)
            return (b[1] << 16) | b[2] | (b[3] << 8)
        if op == 0xDB and b[1] == 0x00:                     # rets
            return self.stack.pop()
        raise Unsupported("команда %s по 0x%06X" % (b.hex(" "), pc))


def run_hook(image, ram, limit=200):
    """От 0x83C116 до развилки. True -- отключить подачу (0x83C11C)."""
    cpu = Cpu(image, ram)
    pc = 0x83C116
    for _ in range(limit):
        if pc == 0x83C11C:
            return True, cpu
        if pc == 0x83C120:
            return False, cpu
        pc = cpu.step(pc)
    raise AssertionError("не дошли до развилки")


def make_ram(nmot, v, wdk, tmot, e_vss=0, e_tps=0, fd16_5=0, fd6e_5=0):
    ram = {}

    def w16(a, val):
        ram[a], ram[a + 1] = val & 0xFF, val >> 8
    w16(0xF8A0, nmot)
    ram[0x881D] = v
    ram[0x89F3] = wdk
    ram[0x8AB4] = tmot
    w16(0xB37C, 0x0002 | e_vss)       # бит 1 -- «проверено», как в коде
    w16(0xB304, 0x0002 | e_tps)
    w16(0xFD16, 0x0002 | (fd16_5 << 5))
    w16(0xFD6E, 0x0040 | (fd6e_5 << 5))
    return ram


def model(p, nmot, v, wdk, tmot, e_vss, e_tps, fd16_5, fd6e_5):
    if fd6e_5:
        return True
    if e_vss or e_tps:
        return False
    if v > p["LCVMAX"] or wdk < p["LCWDKMN"] or tmot < p["LCTMOTMN"]:
        return False
    thr = p["LCNMAX"]
    if fd16_5:
        thr -= p["LCDNH"]
        if thr < 0:
            return False
    return nmot > thr


# --------------------------------------------------------------------------

def test_code_basis(stock):
    print("\n-- опора в стоковом коде")
    t = body(stock, 0x3C0FC, 0x3C132)
    check("bset 0xff10.6" in t and "bmov 0xfd16.5, 0xff10.6" in t
          and "movb RL4, #0x4" in t and "movb 0x8874, RL4" in t,
          "0xFD16.5 -> 0x8874 = 4: отключение подачи на всех четырёх цилиндрах")
    check("jb 0xfd6e.5, 0x83c11c" in t and "jmpr cc_UC, 0x83c120" in t,
          "на 0x83C116 стоит последнее условие -- перегрев 0xFD6E.5")
    targets = [x for _a, x in text(stock, 0x3C0A4, 0x3C224) if x.endswith("0x83c11a")]
    check(not targets, "на 0x83C11A никто не прыгает -- две команды можно "
                       "заменять вместе")
    t = body(stock, 0x61568, 0x615B8)
    check("extp 0x206, #0x1" in t and "movbz r12, 0x1aa4" in t
          and "bmov 0xfd6e.5, 0xff10.6" in t,
          "0xFD6E.5 считается от NOVH (0x19AA4) -- стоковая отсечка по перегреву")
    t = body(stock, 0x2FCB2, 0x2FCE4)
    check("mov r4, #0xa0" in t and "divu r4" in t and "movb 0x881d, RL4" in t,
          "0x881D = сырая скорость / 160 -- км/ч с шагом 1.25")
    t = body(stock, 0x3BEEA, 0x3BF00)
    check("mov r4, 0xb37c" in t and "jnb r4.0" in t,
          "0xB37C.0 -- неисправность датчика скорости (по ней штатно NMAXDV)")
    t = body(stock, 0x50668, 0x50692)
    check("mov r5, 0xb304" in t and "jnb r5.0" in t and "movb 0x89f3, RL4" in t,
          "0xB304.0 -- неисправность дросселя (при ней 0x89F3 подменяется)")
    t = body(stock, 0x3C1D8, 0x3C1E2)
    check("movb RL4, 0x8875" in t and "cmpb RL4, 0x8873" in t,
          "отключение цилиндров по моменту ограничено порогом REDZEM (0x8875)")
    redzem = list(stock[0x183E4:0x183E9])
    check(stock[0x183DE] == 5 and redzem == [5] * 5,
          "REDZEM = 5 при любой ОЖ: ступень (0x9160 + 500..600)/1000 не "
          "больше 4 -- по моменту подача не отключается (сложение -- в ПЗУ)")
    t = body(stock, 0x3BF2E, 0x3BF4C)
    check("mov r12, 0x9158" in t and "mov r13, 0x4bb2" in t
          and "calls 0x0067d8" in t and "bset 0xfd16.2" in t
          and "bset 0xfd16.1" in t and "mov 0x9154, ZEROS" in t,
          "жёсткая ветвь ограничителя: 0xFD16.2, 0xFD16.1 и момент 0x9154 = 0")
    reads = [x for _a, x in text(stock, 0x20000, 0x6C000)
             if "fd16.2" in x and not x.startswith(("bset", "bclr"))]
    check(not reads, "0xFD16.2 в образе никто не читает")
    t = body(stock, 0x3BF6A, 0x3BF7E)
    check("mov r13, 0xf8a0" in t and "calls 0x0067d8" in t,
          "0x0067d8 прибавляет: им же к оборотам добавляется прогноз")


def test_injectors(stock):
    print("\n-- от 0x8874 до форсунок")
    w16 = lambda o: stock[o] | (stock[o + 1] << 8)              # noqa: E731
    check(w16(0x1291C + 2 * 3) == 0x4E56 and w16(0x12912) == 0x4CC0,
          "автомат 0x854CA2: состояние 3 ведёт на сборку маски 0x854E56")
    t = body(stock, 0x54E56, 0x54E7C)
    check("movb RL4, [r5+#0x1916]" in t and "movb 0x8a20, RL4" in t,
          "маска гашения 0x8A20 берётся из таблицы шаблонов 0x11916")
    t = body(stock, 0x54F8A, 0x54FA2)
    check("movb 0x8a21, ONES" in t and "movb 0x8a21, RL4" in t,
          "итоговая маска 0x8A21 = 0x8A20 (или 0xFF при 0x9869)")
    t = body(stock, 0x416F0, 0x416F8)
    check("movbz r4, 0x8a21" in t and "mov 0xfd8a, r4" in t,
          "планировщик впрыска копирует маску в 0xFD8A")
    writes = [x for _a, x in text(stock, 0x20000, 0x6C000) if x.startswith(
        ("mov 0xfd8a", "movb 0xfd8a", "bset 0xfd8a", "bclr 0xfd8a", "bmov 0xfd8a"))]
    check(writes == ["mov 0xfd8a, r4"], "других записей 0xFD8A нет")
    t = body(stock, 0x41942, 0x4195A)
    check("jnb 0xfd8a.0, 0x84195a" in t and "mov r5, T7" in t
          and "sub r5, #0x1" in t and "mov CC30, r5" in t,
          "бит 0xFD8A.0 стоит -> CC30 = T7-1: событие форсунки отменено")
    t = body(stock, 0x41942, 0x41B00)
    check(all("jnb 0xfd8a.%d" % b in t for b in range(4)),
          "так же гасятся остальные три цилиндра (0xFD8A.1..3)")
    seg0 = [x for _a, x in text(stock, 0x54BB8, 0x54FA2) + text(stock, 0x416F0, 0x42520)
            if x.startswith(("calls 0x00", "jmps 0x00"))]
    check(not seg0, "на пути автомат -> планировщик нет вызовов во внутреннее ПЗУ")
    tables = []
    for p in sorted(glob.glob(os.path.join(ROOT, "firmware", "*.bin"))):
        d = open(p, "rb").read()
        if launch.state(d) in ("stock", "patched"):
            tables.append(set(d[0x11916:0x11936]) == {0xFF})
    check(tables and all(tables),
          "таблица 0x11916 сплошь 0xFF во всех FBH3ID60: любое 0x8874 > 0 "
          "гасит все четыре цилиндра")


def test_rom(stock):
    print("\n-- внешняя флешка и внутреннее ПЗУ")
    w16 = lambda d, o: d[o] | (d[o + 1] << 8)                   # noqa: E731
    check(w16(stock, 0x8000) == 0x3012 and w16(stock, 0x8002) == 0x0083,
          "заголовок 0x808000: точка входа 0x833012 -- первые 64 КБ прочитаны")
    t = body(stock, 0x3301A, 0x3302C)
    check("jnb 0xff12.10" in t and "mov SYSCON, #0xe60c" in t,
          "старт 0x833012 пишет SYSCON, сохраняя ROMEN (внутреннее ПЗУ в сегменте 0)")
    lada = fw("B103eq09.bin")
    rec = lambda d: [d[0x1FC00 + k] for k in range(32)]          # noqa: E731
    check(rec(stock) == rec(lada) and w16(stock, 0x1FC08) == 0xF5CF,
          "записи сумм на 0x0000..0x7FFF одинаковы у Kia и ВАЗ B103EQ09 -- "
          "одно и то же ПЗУ процессора")
    targets = {x.split()[1] for _a, x in text(stock, 0x20000, 0x6C000)
               if x.startswith("calls 0x00")}
    check(targets and all(0x0700 <= int(a, 16) < 0x8000 for a in targets),
          "все %d процедур сегмента 0 лежат в 0x0000..0x7FFF" % len(targets))


def test_bytes():
    print("\n-- байты подпрограммы")
    code = launch.assemble()
    check(len(code) == launch.CODE_LEN == 0x4E, "длина 78 байт")
    expect = [
        "jb 0xfd6e.5, 0x86fc4a",
        "mov r4, 0xb37c", "jb r4.0, 0x86fc46",
        "mov r4, 0xb304", "jb r4.0, 0x86fc46",
        "movb RL4, 0x881d", "cmpb RL4, 0x7004", "jmpr cc_UGT, 0x86fc46",
        "movb RL4, 0x89f3", "cmpb RL4, 0x7005", "jmpr cc_C, 0x86fc46",
        "movb RL4, 0x8ab4", "cmpb RL4, 0x7006", "jmpr cc_C, 0x86fc46",
        "mov r4, 0x7000", "jnb 0xfd16.5, 0x86fc40",
        "sub r4, 0x7002", "jmpr cc_C, 0x86fc46",
        "cmp r4, 0xf8a0", "jmpr cc_C, 0x86fc4a",
        "mov r4, #0x0", "rets",
        "mov r4, #0x1", "rets",
    ]
    dis = c166dis.Disassembler()
    got = [" ".join(i.text().split())
           for i in c166dis.disassemble(code, 0, len(code), base=launch.CODE_ADDR,
                                        dis=dis)]
    check(got == expect, "дизассемблер (из c166.sinc) читает ровно задуманное")
    if got != expect:
        for g, e in zip(got, expect):
            if g != e:
                print("     было %r, ждали %r" % (g, e))
    hook = launch.hook_bytes()
    got = [" ".join(i.text().split())
           for i in c166dis.disassemble(hook, 0, 6, base=0x83C116, dis=dis)]
    check(got == ["calls 0x86fc00", "jmpr cc_EQ, 0x83c120"],
          "врезка: calls 0x86fc00 / jmpr cc_EQ, 0x83c120")
    check(launch.dpp1(launch.PARAMS) == 0x7000 and launch.dpp1(0x14BB4) == 0x4BB4,
          "параметры адресуются через DPP1 так же, как NMAX (0x4BB4)")


def test_behaviour(stock):
    print("\n-- поведение")
    rng = random.Random(40)
    sets = [launch.default_raw(),
            {"LCNMAX": 3500 * 4, "LCDNH": 150 * 4, "LCVMAX": 0, "LCWDKMN": 128,
             "LCTMOTMN": 100},
            {"LCNMAX": 100, "LCDNH": 400, "LCVMAX": 255, "LCWDKMN": 0,
             "LCTMOTMN": 0}]       # LCDNH > LCNMAX: заём, проверка защиты
    total = bad = 0
    regs_ok = True
    for p in sets:
        img = bytearray(launch.apply(stock, launch.default_raw()))
        launch.write_params(img, p)
        img = bytes(img)
        edges_n = [p["LCNMAX"] - 1, p["LCNMAX"], p["LCNMAX"] + 1,
                   p["LCNMAX"] - p["LCDNH"], p["LCNMAX"] - p["LCDNH"] + 1, 0, 0xFFFF]
        for k in range(3000):
            nmot = rng.choice(edges_n) & 0xFFFF if k % 3 == 0 else rng.randrange(0, 30000)
            v = rng.choice([0, 1, 2, 3, p["LCVMAX"], min(255, p["LCVMAX"] + 1), 255])
            wdk = rng.choice([0, p["LCWDKMN"] - 1 if p["LCWDKMN"] else 0,
                              p["LCWDKMN"], 255, rng.randrange(256)])
            tmot = rng.choice([0, p["LCTMOTMN"] - 1 if p["LCTMOTMN"] else 0,
                               p["LCTMOTMN"], 200, rng.randrange(256)])
            flags = [int(rng.random() < 0.15), int(rng.random() < 0.15),
                     rng.randrange(2), int(rng.random() < 0.1)]
            ram = make_ram(nmot, v, wdk, tmot, *flags)
            got, cpu = run_hook(img, ram)
            want = model(p, nmot, v, wdk, tmot, *flags)
            total += 1
            if got != want:
                bad += 1
                if bad <= 5:
                    print("     расхождение: n=%d v=%d wdk=%d t=%d flags=%s: %s, "
                          "ждали %s" % (nmot, v, wdk, tmot, flags, got, want))
            if cpu.touched - {4} or cpu.stack:
                regs_ok = False
    check(bad == 0, "исполнитель и модель совпали на %d входах из %d"
          % (total - bad, total))
    check(regs_ok, "подпрограмма меняет только r4 и возвращается в 0x83C11A")

    # когда лаунча нет, всё как в стоке
    img = launch.apply(stock, launch.default_raw())
    same = True
    for _ in range(2000):
        flags = [rng.randrange(2) for _ in range(4)]
        ram = make_ram(rng.randrange(30000), rng.randrange(3, 256), rng.randrange(256),
                       rng.randrange(256), *flags)       # скорость > 2.5 км/ч
        if run_hook(img, ram)[0] != run_hook(stock, ram)[0]:
            same = False
    check(same, "на ходу решение совпадает со стоком (перегрев -- да, иначе -- нет)")

    # гистерезис: проезд оборотов вверх и вниз
    p = launch.default_raw()
    seq, state, trace = list(range(15000, 18000, 40)) + list(range(18000, 15000, -40)), 0, []
    for n in seq:
        state = int(run_hook(img, make_ram(n, 0, 255, 180, fd16_5=state))[0])
        trace.append((n, state))
    up = next(n for n, s in trace if s)
    down = next(n for n, s in trace[len(trace) // 2:] if not s)
    check(up == p["LCNMAX"] + 40 - (p["LCNMAX"] % 40) and
          down <= p["LCNMAX"] - p["LCDNH"],
          "отсечка с %.0f об/мин, возврат подачи с %.0f (порог %.0f/%.0f)"
          % (up / 4, down / 4, p["LCNMAX"] / 4, (p["LCNMAX"] - p["LCDNH"]) / 4))


def test_images():
    print("\n-- образы")
    paths = [p for p in sorted(glob.glob(os.path.join(ROOT, "firmware", "*.bin")))
             if os.path.getsize(p) == launch.SIZE]
    fbh = [p for p in paths if launch.state(open(p, "rb").read()) in ("stock", "patched")]
    names = {os.path.basename(p) for p in fbh}
    check({"FBH3ID60_stok.bin", "FBH3ID60_v2M.bin", "FBH3ID60_mpower.bin"} <= names,
          "ставится на все FBH3ID60 (%d файлов)" % len(fbh))
    check(launch.state(fw("B103eq09.bin")) != "stock",
          "на чужой блок (B103eq09) не ставится")
    ok_round = ok_sum = ok_diff = True
    allowed = (set(range(launch.HOOK, launch.HOOK + 6)) |
               set(range(launch.CODE, launch.CODE + launch.CODE_LEN)) |
               set(range(launch.PARAMS, launch.PARAMS + launch.PARAMS_LEN - 1)))
    for p in fbh:
        d = open(p, "rb").read()
        if launch.state(d) != "stock":
            continue
        raw = launch.apply(d, launch.default_raw())
        diff = {i for i in range(len(d)) if d[i] != raw[i]}
        ok_diff &= diff <= allowed
        out, _fixed, good = launch.fix_sums(raw)
        ok_sum &= good == 32
        back, _f, _g = launch.fix_sums(launch.remove(bytes(out)))
        ok_round &= bytes(back) == d
    check(ok_diff, "до пересчёта сумм меняются только врезка, код и параметры")
    check(ok_sum, "после пересчёта сумм проверка 32 из 32")
    check(ok_round, "remove возвращает исходный файл байт в байт")

    built = os.path.join(ROOT, "firmware", "FBH3ID60_v2M_lc.bin")
    if os.path.exists(built):
        d = open(built, "rb").read()
        want, _f, _g = launch.fix_sums(launch.apply(fw("FBH3ID60_v2M.bin"),
                                                    launch.default_raw()))
        check(d == bytes(want), "FBH3ID60_v2M_lc.bin = v2M + лаунч по умолчанию")
        bad, _sk, good = bosch_csum.check(fwlib.Firmware(d), bosch_csum.TABLE_ADDR,
                                          verbose=False)
        check(not bad and len(good) == 32, "FBH3ID60_v2M_lc.bin: суммы 32 из 32")


def main():
    stock = fw("FBH3ID60_stok.bin")
    test_code_basis(stock)
    test_injectors(stock)
    test_rom(stock)
    test_bytes()
    test_behaviour(stock)
    test_images()
    print()
    if fails:
        print("ПРОВАЛЕНО: %d" % len(fails))
        sys.exit(1)
    print("Всё сошлось.")


if __name__ == "__main__":
    main()
