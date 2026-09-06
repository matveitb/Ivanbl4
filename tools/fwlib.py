"""
fwlib -- общие примитивы для анализа прошивок Bosch M7.9.7 (Infineon C167).

C167 -- little-endian, 16-битное ядро. Все многобайтные значения в прошивке
по умолчанию читаются как little-endian.

Модуль намеренно без внешних зависимостей (нет numpy) -- 512 КБ обрабатывается
чистым Python за секунды.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Sequence


# --------------------------------------------------------------------------
# Загрузка образа
# --------------------------------------------------------------------------

@dataclass
class Firmware:
    """Загруженный образ прошивки."""

    data: bytes
    path: str = "<memory>"
    base: int = 0  # адрес, по которому образ отображается в адресном пространстве

    @property
    def size(self) -> int:
        return len(self.data)

    def __len__(self) -> int:
        return len(self.data)

    def __getitem__(self, item):
        return self.data[item]

    # -- чтение скаляров -------------------------------------------------

    def u8(self, off: int) -> int:
        return self.data[off]

    def u16(self, off: int) -> int:
        return self.data[off] | (self.data[off + 1] << 8)

    def u32(self, off: int) -> int:
        return struct.unpack_from("<I", self.data, off)[0]

    def s8(self, off: int) -> int:
        v = self.data[off]
        return v - 256 if v >= 128 else v

    def s16(self, off: int) -> int:
        v = self.u16(off)
        return v - 65536 if v >= 32768 else v

    # -- чтение векторов -------------------------------------------------

    def vec(self, off: int, count: int, width: int, signed: bool = False) -> list[int]:
        """Прочитать count значений разрядности width (1 или 2) байт."""
        if width == 1:
            raw = self.data[off:off + count]
            if len(raw) != count:
                raise IndexError("выход за границу образа")
            if signed:
                return [b - 256 if b >= 128 else b for b in raw]
            return list(raw)
        if width == 2:
            end = off + count * 2
            if end > len(self.data):
                raise IndexError("выход за границу образа")
            fmt = "<%d%s" % (count, "h" if signed else "H")
            return list(struct.unpack_from(fmt, self.data, off))
        raise ValueError("width должен быть 1 или 2")

    def in_range(self, off: int, length: int) -> bool:
        return 0 <= off and off + length <= len(self.data)


def load(path: str, base: int = 0) -> Firmware:
    with open(path, "rb") as fh:
        return Firmware(fh.read(), path=path, base=base)


# --------------------------------------------------------------------------
# Статистика по блокам
# --------------------------------------------------------------------------

def entropy(chunk: Sequence[int]) -> float:
    """Энтропия Шеннона в битах на байт (0..8)."""
    if not chunk:
        return 0.0
    counts = [0] * 256
    for b in chunk:
        counts[b] += 1
    n = float(len(chunk))
    acc = 0.0
    for c in counts:
        if c:
            p = c / n
            acc -= p * _log2(p)
    return acc


def _log2(x: float) -> float:
    import math
    return math.log(x, 2.0)


def is_blank(chunk: bytes) -> bool:
    """Стёртая флеш-область (все 0xFF) либо сплошные нули."""
    if not chunk:
        return False
    first = chunk[0]
    return first in (0x00, 0xFF) and chunk.count(first) == len(chunk)


# --------------------------------------------------------------------------
# Монотонность / гладкость -- основа детекта осей и карт
# --------------------------------------------------------------------------

def is_monotonic_increasing(vals: Sequence[int], strict: bool = True) -> bool:
    if len(vals) < 2:
        return False
    if strict:
        return all(b > a for a, b in zip(vals, vals[1:]))
    return all(b >= a for a, b in zip(vals, vals[1:]))


def is_monotonic_decreasing(vals: Sequence[int], strict: bool = True) -> bool:
    if len(vals) < 2:
        return False
    if strict:
        return all(b < a for a, b in zip(vals, vals[1:]))
    return all(b <= a for a, b in zip(vals, vals[1:]))


def mean_abs_second_diff(vals: Sequence[int]) -> float:
    """Средний модуль второй разности -- мера "изломанности" ряда."""
    if len(vals) < 3:
        return 0.0
    acc = 0
    for i in range(1, len(vals) - 1):
        acc += abs(vals[i - 1] - 2 * vals[i] + vals[i + 1])
    return acc / float(len(vals) - 2)


def smoothness(matrix: Sequence[Sequence[int]]) -> float:
    """
    Оценка гладкости таблицы в диапазоне 0..1 (1 = идеально гладкая).

    Настоящие калибровочные карты гладкие: вторая разность мала по сравнению
    с размахом значений. Случайный код/данные дают близкое к нулю значение.
    """
    flat = [v for row in matrix for v in row]
    if len(flat) < 4:
        return 0.0
    lo, hi = min(flat), max(flat)
    span = hi - lo
    if span == 0:
        # константная таблица -- формально гладкая, но неинформативная
        return 0.0

    acc, cnt = 0.0, 0
    for row in matrix:
        if len(row) >= 3:
            acc += mean_abs_second_diff(row)
            cnt += 1
    # то же по столбцам
    if matrix and len(matrix) >= 3:
        ncols = len(matrix[0])
        for c in range(ncols):
            col = [row[c] for row in matrix if c < len(row)]
            if len(col) >= 3:
                acc += mean_abs_second_diff(col)
                cnt += 1
    if cnt == 0:
        return 0.0
    avg = acc / cnt
    # нормируем размахом; 1.0 -> идеально линейная поверхность
    score = 1.0 - min(1.0, avg / (span * 0.5))
    return max(0.0, score)


def reshape(flat: Sequence[int], rows: int, cols: int) -> list[list[int]]:
    return [list(flat[r * cols:(r + 1) * cols]) for r in range(rows)]


# --------------------------------------------------------------------------
# Поиск ссылок (грубый xref)
# --------------------------------------------------------------------------

def find_le16_refs(fw: Firmware, value: int, limit: int = 64) -> list[int]:
    """
    Найти все вхождения 16-битного little-endian значения.

    Для C167 адресация внутри сегмента идёт 16-битным смещением через DPP,
    поэтому младшие 16 бит адреса карты часто встречаются в коде как
    непосредственный операнд -- это дешёвый способ подтвердить кандидата.
    """
    needle = struct.pack("<H", value & 0xFFFF)
    out: list[int] = []
    start = 0
    while len(out) < limit:
        idx = fw.data.find(needle, start)
        if idx < 0:
            break
        out.append(idx)
        start = idx + 1
    return out


# --------------------------------------------------------------------------
# Вспомогательное форматирование
# --------------------------------------------------------------------------

def hexa(addr: int) -> str:
    return "0x%06X" % addr


def fmt_matrix(matrix: Sequence[Sequence[int]], width: int = 6) -> str:
    lines = []
    for row in matrix:
        lines.append(" ".join(("%%%dd" % width) % v for v in row))
    return "\n".join(lines)


def human_size(n: int) -> str:
    for unit in ("Б", "КБ", "МБ"):
        if n < 1024 or unit == "МБ":
            return "%.0f %s" % (n, unit) if unit == "Б" else "%.1f %s" % (n, unit)
        n /= 1024.0
    return str(n)
