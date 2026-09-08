#!/usr/bin/env python3
"""
accel -- прикидка разгона по кривой наполнения из прошивки.

Берёт RLVMXN (максимальное наполнение при полностью открытом дросселе,
0x182B6: байт счётчика, 11 точек оси x40 об/мин, 11 значений x0.75 %) и
считает по ней форму кривой момента: момент пропорционален наполнению.
Дальше интегрирует разгон на выбранной передаче.

Нужно, чтобы отличить настоящий провал от нормальной нехватки мощности:
если расчёт даёт те же секунды, что чувствуются в машине, искать в
прошивке нечего.

    python3 tools/accel.py firmware/FBH3ID60_stok.bin --from 2000 --to 2700

Параметры машины -- оценочные, их можно переопределить ключами. Результат
чувствителен к массе и передаточным числам, но не настолько, чтобы менять
вывод: ошибка в 30 % по любому из них не превращает три секунды в одну.
"""

from __future__ import annotations

import argparse
import math
import sys

ADDR = 0x182B6          # RLVMXN
AX_FACTOR = 40.0        # об/мин на единицу оси
VAL_FACTOR = 0.75       # % наполнения на единицу


def rlvmxn(data: bytes) -> tuple[list[float], list[float]]:
    n = data[ADDR]
    rpm = [data[ADDR + 1 + i] * AX_FACTOR for i in range(n)]
    rl = [data[ADDR + 1 + n + i] * VAL_FACTOR for i in range(n)]
    return rpm, rl


def interp(x: float, xs: list[float], ys: list[float]) -> float:
    if x <= xs[0]:
        return ys[0]
    for i in range(len(xs) - 1):
        if xs[i] <= x <= xs[i + 1]:
            k = (x - xs[i]) / (xs[i + 1] - xs[i])
            return ys[i] + k * (ys[i + 1] - ys[i])
    return ys[-1]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Прикидка разгона по RLVMXN")
    ap.add_argument("firmware")
    ap.add_argument("--from", dest="n0", type=float, default=2000.0, help="от, об/мин")
    ap.add_argument("--to", dest="n1", type=float, default=2700.0, help="до, об/мин")
    ap.add_argument("--gear", type=float, default=1.32, help="передаточное число передачи")
    ap.add_argument("--final", type=float, default=4.06, help="главная пара")
    ap.add_argument("--mass", type=float, default=1250.0, help="масса с водителем, кг")
    ap.add_argument("--radius", type=float, default=0.298, help="радиус колеса, м")
    ap.add_argument("--torque", type=float, default=145.0, help="паспортный максимум момента, Н*м")
    ap.add_argument("--cda", type=float, default=0.66, help="Cx * площадь, м^2")
    ap.add_argument("--table", action="store_true", help="показать таблицу момента и мощности")
    a = ap.parse_args(argv)

    rpm, rl = rlvmxn(open(a.firmware, "rb").read())
    peak = max(rl)
    ratio = a.gear * a.final

    if a.table:
        pw = [r * n for r, n in zip(rl, rpm)]
        pmax = max(pw)
        print(f"{'об/мин':>7} {'rl,%':>7} {'момент,% от max':>17} {'мощность,% от max':>19}")
        for n, v, p in zip(rpm, rl, pw):
            print(f"{n:7.0f} {v:7.2f} {100 * v / peak:17.0f} {100 * p / pmax:19.0f}")
        print()

    def speed(n: float) -> float:
        return n / 60 * 2 * math.pi / ratio * a.radius

    v, v1, t, dt = speed(a.n0), speed(a.n1), 0.0, 0.005
    while v < v1 and t < 60:
        n = v / a.radius * ratio * 60 / (2 * math.pi)
        moment = a.torque * interp(n, rpm, rl) / peak
        force = moment * ratio * 0.95 / a.radius
        drag = a.mass * 9.81 * 0.013 + 0.5 * 1.2 * a.cda * v * v
        v += (force - drag) / (a.mass * 1.12) * dt
        t += dt

    print(f"передача {a.gear} x {a.final}, масса {a.mass:.0f} кг")
    print(f"  {a.n0:.0f} об/мин = {speed(a.n0) * 3.6:.1f} км/ч, "
          f"{a.n1:.0f} = {speed(a.n1) * 3.6:.1f} км/ч")
    print(f"  момент на {a.n0:.0f} / {a.n1:.0f}: "
          f"{a.torque * interp(a.n0, rpm, rl) / peak:.0f} / "
          f"{a.torque * interp(a.n1, rpm, rl) / peak:.0f} Н*м из {a.torque:.0f}")
    print(f"  время при полном газе: {t:.1f} с")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
