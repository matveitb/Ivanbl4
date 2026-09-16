#!/usr/bin/env python3
"""
Чтение и запись значений карты.

Правило пересчёта берётся из COMPU_METHOD и живёт в MapLayout:

    физическое = сырое * множитель + смещение
    сырое      = (физическое - смещение) / множитель

Про округление и зажим. Обратный пересчёт почти никогда не даёт целое,
поэтому результат округляется к ближайшему и зажимается в диапазон типа.
Это значит, что задать можно не любое число: шаг сетки равен множителю.
Редактор обязан показывать ЧТО ЛЕГЛО, а не что ввели -- иначе человек
думает, что поставил 14.3, а в файле 14.25.

Порядок ячеек. FNC_VALUES в ASAP2 объявляется как ROW_DIR или COLUMN_DIR.
При ROW_DIR строка идёт подряд (обычный случай), при COLUMN_DIR подряд
идёт столбец. Путать нельзя: на квадратной карте ошибка не заметна
глазом, но значения окажутся транспонированными.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "editor", "a2l"))

from geometry import MapLayout                              # noqa: E402


def cell_offset(lay: MapLayout, r: int, c: int) -> int:
    if not (0 <= r < lay.ny and 0 <= c < lay.nx):
        raise IndexError("ячейка (%d, %d) вне карты %dx%d"
                         % (r, c, lay.ny, lay.nx))
    idx = r * lay.nx + c if lay.row_dir else c * lay.ny + r
    return lay.data_off + idx * lay.width


def get_raw(buf, lay: MapLayout, r: int, c: int) -> int:
    a = cell_offset(lay, r, c)
    return int.from_bytes(buf[a:a + lay.width], "little", signed=lay.signed)


def set_raw(buf: bytearray, lay: MapLayout, r: int, c: int, raw: int) -> None:
    lo, hi = lay.raw_range()
    raw = max(lo, min(hi, int(raw)))
    a = cell_offset(lay, r, c)
    buf[a:a + lay.width] = raw.to_bytes(lay.width, "little", signed=lay.signed)


def raw_to_phys(lay: MapLayout, raw: float) -> float:
    return raw * lay.factor + lay.offset


def phys_to_raw(lay: MapLayout, value: float) -> int:
    """Физическое в сырое: округление к ближайшему и зажим в тип."""
    if not lay.factor:
        return 0
    raw = round((value - lay.offset) / lay.factor)
    lo, hi = lay.raw_range()
    return max(lo, min(hi, int(raw)))


def snap(lay: MapLayout, value: float) -> float:
    """Что РЕАЛЬНО ляжет в файл при попытке записать это значение."""
    return raw_to_phys(lay, phys_to_raw(lay, value))


def read_raw(buf, lay: MapLayout) -> list[list[int]]:
    return [[get_raw(buf, lay, r, c) for c in range(lay.nx)]
            for r in range(lay.ny)]


def read_phys(buf, lay: MapLayout) -> list[list[float]]:
    return [[raw_to_phys(lay, v) for v in row] for row in read_raw(buf, lay)]


def write_phys(buf: bytearray, lay: MapLayout, r: int, c: int,
               value: float) -> int:
    raw = phys_to_raw(lay, value)
    set_raw(buf, lay, r, c, raw)
    return raw


def axes(buf, lay: MapLayout) -> tuple[list[float], list[float]]:
    return lay.x_axis.values(buf), lay.y_axis.values(buf)


def fmt_table(buf, lay: MapLayout, width: int = 8, digits: int = 2) -> str:
    """Текстовая таблица -- для проверки ядра без окна."""
    xs, ys = axes(buf, lay)
    vals = read_phys(buf, lay)
    head = " " * 9 + "".join(("%%%d.%df" % (width, 1)) % x
                             for x in xs[:lay.nx])
    lines = [head]
    for r in range(lay.ny):
        y = ys[r] if r < len(ys) else r
        lines.append(("%8.0f " % y)
                     + "".join(("%%%d.%df" % (width, digits)) % v
                               for v in vals[r]))
    return "\n".join(lines)
