#!/usr/bin/env python3
"""
expect_maf -- сколько килограммов воздуха в час ДОЛЖЕН показывать ДМРВ.

Зачем. Спор про провал упирается в вопрос, который до сих пор решался на
слух: воздух в мотор реально идёт или нет. Теперь на него можно ответить
числом, потому что найдены обе величины, из которых оно считается:

    KUMSRL  0x1157C  -- константа пересчёта расхода в наполнение
    RLVMXN  0x182B6  -- максимальное наполнение при открытом дросселе

Расход связан с наполнением жёстко:

    расход [кг/ч] = наполнение [%] * обороты [1/мин] * KUMSRL

Значит для каждой точки оборотов при газе в пол известно, что обязан
показывать ДМРВ. Снимаете лог, сравниваете. Если в полосе провала
измеренный расход заметно ниже расчётного -- воздуха действительно нет,
и дело не в калибровке: ищите ДМРВ, впуск, выпуск, фазы ГРМ. Если
совпадает -- воздух есть, и копать надо топливо и угол.

    python3 tools/expect_maf.py firmware/FBH3ID60_stok.bin
    python3 tools/expect_maf.py firmware/FBH3ID60_stok.bin --measured 2500=95
"""

from __future__ import annotations

import argparse
import sys

KUMSRL_ADDR = 0x1157C
KUMSRL_F = 7.8125e-06
RLVMXN_ADDR = 0x182B6
RL_F = 0.75                      # rel_ub_q0p75
RPM_F = 40.0
RHO = 1.293


def read_curve(fw: bytes, addr: int):
    n = fw[addr]
    axis = [fw[addr + 1 + i] * RPM_F for i in range(n)]
    data = [fw[addr + 1 + n + i] * RL_F for i in range(n)]
    return axis, data


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Расчётный расход воздуха при полном газе")
    ap.add_argument("firmware")
    ap.add_argument("--measured", action="append", default=[],
                    help="замер из лога: обороты=расход, можно несколько")
    a = ap.parse_args(argv)

    fw = open(a.firmware, "rb").read()
    kum = fw[KUMSRL_ADDR] * KUMSRL_F
    disp = kum * 1e5 / (RHO * 30)
    rpm, rl = read_curve(fw, RLVMXN_ADDR)

    print("KUMSRL 0x%05X = %d -> %.7g (кг/ч)/(1/мин)/%%" % (
        KUMSRL_ADDR, fw[KUMSRL_ADDR], kum))
    print("отсюда рабочий объём %.4f л\n" % disp)
    print("%-9s %-12s %-14s %s" % ("об/мин", "нагрузка %", "расход кг/ч", "г/с"))
    for n, r in zip(rpm, rl):
        ms = r * n * kum
        print("%-9.0f %-12.2f %-14.1f %.1f" % (n, r, ms, ms / 3.6))

    meas = []
    for m in a.measured:
        try:
            k, v = m.split("=")
            meas.append((float(k), float(v)))
        except ValueError:
            print("не понял замер: " + m, file=sys.stderr)
            return 1
    if meas:
        print("\nСверка с логом:")
        print("%-9s %-12s %-12s %s" % ("об/мин", "ожидалось", "замер", "доля"))
        for n, got in meas:
            # линейная интерполяция ожидаемой нагрузки по RLVMXN
            r = rl[0] if n <= rpm[0] else rl[-1]
            for i in range(len(rpm) - 1):
                if rpm[i] <= n <= rpm[i + 1]:
                    t = (n - rpm[i]) / (rpm[i + 1] - rpm[i])
                    r = rl[i] + t * (rl[i + 1] - rl[i])
                    break
            want = r * n * kum
            share = got / want * 100 if want else 0
            verdict = ("воздух есть" if share >= 90 else
                       "ВОЗДУХА НЕ ХВАТАЕТ" if share < 75 else "низковато")
            print("%-9.0f %-12.1f %-12.1f %.0f %%  %s"
                  % (n, want, got, share, verdict))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
