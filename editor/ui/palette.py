#!/usr/bin/env python3
"""
Одна палитра на всё: заливка ячеек таблицы, кривая, поверхность.

Синий -- малое, зелёный -- среднее, жёлтый и красный -- большое. Так
принято в тюнинговых редакторах, и главное -- так карта читается формой,
а не чтением восьмидесяти чисел подряд.

Отдельно: цвет берётся от размаха ИМЕННО ЭТОЙ карты, а не от диапазона
типа. У карты УОЗ значения 0..45 градусов при диапазоне байта -128..127;
крась мы по типу, вся карта была бы одного оттенка и заливка не говорила
бы ничего.
"""

from __future__ import annotations

from PySide6 import QtGui

STOPS = [
    (0.00, (40, 70, 160)),
    (0.30, (40, 150, 150)),
    (0.55, (90, 175, 80)),
    (0.78, (225, 190, 60)),
    (1.00, (205, 70, 55)),
]


def heat(t: float) -> QtGui.QColor:
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    for i in range(len(STOPS) - 1):
        a, ca = STOPS[i]
        b, cb = STOPS[i + 1]
        if a <= t <= b:
            k = (t - a) / (b - a) if b > a else 0.0
            return QtGui.QColor(*[round(ca[j] + (cb[j] - ca[j]) * k)
                                  for j in range(3)])
    return QtGui.QColor(*STOPS[-1][1])


def text_on(c: QtGui.QColor) -> QtGui.QColor:
    """Чёрный или белый -- смотря что читается поверх заливки."""
    lum = 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()
    return QtGui.QColor(20, 20, 20) if lum > 140 else QtGui.QColor(245, 245, 245)


def span(values) -> tuple[float, float]:
    flat = [v for row in values for v in row]
    if not flat:
        return 0.0, 1.0
    lo, hi = min(flat), max(flat)
    return (lo, hi) if hi > lo else (lo, lo + 1.0)
