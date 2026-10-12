#!/usr/bin/env python3
"""
OBD-II режим 01: что спрашивать у блока и как пересчитывать ответ.

Пересчёты -- стандарт SAE J1979, и для нашего блока они сверены с кодом
обработчика режима 01 (0x864996, docs/40): какая ячейка ОЗУ уходит в
ответ и как она пересчитывается. Имена каналов -- те же, что пишет
Car Scanner: тогда лог нашего логгера читается тем же разбором, что и
лог Car Scanner, и поправка по нему считается без переделок.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Pid:
    pid: int
    name: str           # имя канала в логе
    unit: str
    nbytes: int
    decode: Callable    # bytes -> float | None
    title: str = ""     # по-русски, для окна


def _u16(b) -> int:
    return (b[0] << 8) | b[1]


def _fuel_status(b) -> float | None:
    """
    PID 0x03, первый байт: 1 -- разомкнутый (ещё не готов), 2 --
    замкнутый по зонду, 4 -- разомкнутый по нагрузке или сбросу топлива,
    8 -- разомкнутый из-за неисправности, 16 -- замкнутый с неисправностью.
    """
    return float(b[0])


PIDS = {p.pid: p for p in (
    Pid(0x03, "Fuel system status", "", 2, _fuel_status,
        "контур топлива"),
    Pid(0x04, "Calculated engine load", "%", 1,
        lambda b: b[0] * 100 / 255, "расчётная нагрузка"),
    Pid(0x05, "Engine coolant temperature", "°C", 1,
        lambda b: b[0] - 40.0, "температура ОЖ"),
    Pid(0x06, "Short term fuel % trim - Bank 1", "%", 1,
        lambda b: (b[0] - 128) * 100 / 128, "STFT"),
    Pid(0x07, "Long term fuel % trim - Bank 1", "%", 1,
        lambda b: (b[0] - 128) * 100 / 128, "LTFT"),
    Pid(0x0C, "Engine RPM", "rpm", 2,
        lambda b: _u16(b) / 4.0, "обороты"),
    Pid(0x0D, "Vehicle speed", "km/h", 1,
        lambda b: float(b[0]), "скорость"),
    Pid(0x0E, "Timing advance", "°", 1,
        lambda b: b[0] / 2.0 - 64, "угол"),
    Pid(0x0F, "Intake air temperature", "°C", 1,
        lambda b: b[0] - 40.0, "температура воздуха"),
    Pid(0x10, "Mass air flow rate", "g/s", 2,
        lambda b: _u16(b) / 100.0, "расход воздуха"),
    Pid(0x11, "Throttle position", "%", 1,
        lambda b: b[0] * 100 / 255, "дроссель"),
    Pid(0x14, "O2 sensor 1 voltage", "V", 2,
        lambda b: b[0] / 200.0, "узкий зонд"),
)}

@dataclass(frozen=True)
class Did:
    """Режим 22: запрос «22 12 XX», ответ «62 12 XX данные»."""
    did: int
    name: str
    unit: str
    nbytes: int
    decode: Callable
    title: str = ""


# Режим 22 найден по коду блока (обработчик 0x8656D6, docs/41): то же, что
# режим 01, но без огрубления OBD, и сверх того -- сырой АЦП ДМРВ и флаг
# работы регулятора по зонду. Есть ли он на связи у конкретной машины,
# выясняется при подключении: не ответил -- не спрашиваем.
DIDS = {d.did: d for d in (
    Did(0x1235, "MAF sensor ADC", "counts", 2,
        lambda b: float(_u16(b) & 0x3FF), "АЦП ДМРВ"),
    Did(0x1285, "Air mass flow ECU", "kg/h", 2,
        lambda b: _u16(b) / 10.0, "расход ЭБУ"),
    Did(0x1283, "Relative air charge", "%", 2,
        lambda b: _u16(b) * 0.0234375, "наполнение rl"),
    Did(0x1216, "Closed loop active", "", 1,
        lambda b: float((b[0] >> 1) & 1), "регулятор по зонду"),
)}


def encode22(did: int, value: float) -> bytes:
    """Значение -> байты ответа режима 22 (для имитатора)."""
    def w(x):
        return max(0, min(0xFFFF, int(round(x)))).to_bytes(2, "big")
    if did == 0x1235:
        return w(value)
    if did == 0x1285:
        return w(value * 10)
    if did == 0x1283:
        return w(value / 0.0234375)
    if did == 0x1216:
        return bytes([2 if value else 0])
    raise KeyError("DID %04X не описан" % did)


# Что спрашивать и как часто. Запрос -- (режим, номер): (1, PID) или
# (0x22, DID). Вес -- во сколько раз реже главного: ELM по K-линии делает
# 6-7 запросов в секунду на всех, и делить их поровну между оборотами и
# температурой ОЖ -- расточительство. 1 -- каждый круг, 2 -- через круг.
PLAN_01 = [
    ((1, 0x0C), 1),      # обороты
    ((1, 0x10), 1),      # расход, огрублённый до 0.25 г/с
    ((1, 0x11), 1),      # дроссель
    ((1, 0x06), 2),      # STFT
    ((1, 0x03), 4),      # замкнут ли контур
    ((1, 0x07), 8),      # LTFT -- меняется медленно
    ((1, 0x05), 20),     # ОЖ
]
# Если блок отвечает на режим 22: сырой АЦП ДМРВ -- ключ тарировки без
# поправки пульсаций, -- каждый круг, расход в полном разрешении через
# круг (с АЦП он нужен только для наполнения и порогов).
PLAN_22 = [
    ((0x22, 0x1235), 1),  # АЦП ДМРВ
    ((1, 0x0C), 1),
    ((1, 0x11), 1),
    ((0x22, 0x1285), 2),  # расход, 0.1 кг/ч
    ((1, 0x06), 2),
    ((0x22, 0x1216), 4),  # регулятор по зонду
    ((1, 0x07), 8),
    ((1, 0x05), 20),
]
DEFAULT_PLAN = PLAN_01


def describe(key):
    """(режим, номер) -> описание величины: Pid или Did."""
    mode, ident = key
    return PIDS.get(ident) if mode == 1 else DIDS.get(ident)


def supported_from_bitmaps(bitmaps: dict) -> set:
    """
    Поддерживаемые PID из ответов на 0x00, 0x20, 0x40...: в каждом четыре
    байта, старший бит первого -- PID base+1.
    """
    out = set()
    for base, data in bitmaps.items():
        if not data or len(data) < 4:
            continue
        bits = int.from_bytes(bytes(data[:4]), "big")
        for i in range(32):
            if bits & (1 << (31 - i)):
                out.add(base + 1 + i)
    return out


def bitmap_for(pids: set, base: int) -> bytes:
    """Обратное: четыре байта ответа на PID base (для имитатора)."""
    bits = 0
    for i in range(32):
        if (base + 1 + i) in pids:
            bits |= 1 << (31 - i)
    return bits.to_bytes(4, "big")


def encode(pid: int, value: float) -> bytes:
    """Значение -> байты ответа (для имитатора блока)."""
    def clamp(x, hi=255):
        return max(0, min(hi, int(round(x))))
    if pid == 0x0C:
        return clamp(value * 4, 0xFFFF).to_bytes(2, "big")
    if pid == 0x10:
        return clamp(value * 100, 0xFFFF).to_bytes(2, "big")
    if pid in (0x05, 0x0F):
        return bytes([clamp(value + 40)])
    if pid in (0x06, 0x07):
        return bytes([clamp(value * 128 / 100 + 128)])
    if pid in (0x04, 0x11):
        return bytes([clamp(value * 255 / 100)])
    if pid == 0x0E:
        return bytes([clamp((value + 64) * 2)])
    if pid == 0x0D:
        return bytes([clamp(value)])
    if pid == 0x03:
        return bytes([clamp(value), 0])
    if pid == 0x14:
        return bytes([clamp(value * 200), 0xFF])
    raise KeyError("PID 0x%02X не описан" % pid)
