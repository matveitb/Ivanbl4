#!/usr/bin/env python3
"""
OBD-II режим 01: что спрашивать у блока и как пересчитывать ответ.

Пересчёты -- стандарт SAE J1979, и для нашего блока они сверены с кодом
обработчика режима 01 (0x864996, docs/40): какая ячейка ОЗУ уходит в
ответ и как она пересчитывается. Имена каналов -- те же, что пишет
Car Scanner на английском (сверены с настоящими экспортами: «MAF air flow
rate» в g/sec, «Oxygen sensor 1 Bank 1 Voltage», ℃): тогда лог нашего
логгера читается тем же разбором, что и лог Car Scanner, и поправка по
нему считается без переделок. Второй байт PID 0x14 Car Scanner пишет
отдельным каналом «Oxygen sensor 1 Bank 1 Short term fuel trim»; у нас
он идёт в канал STFT -- в нашем блоке это та же ячейка 0x95AE, что и в
PID 0x06.
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
    # ещё величины из того же ответа: (имя, единицы, decode)
    extra: tuple = ()


def _u16(b) -> int:
    return (b[0] << 8) | b[1]


def _fuel_status(b) -> float | None:
    """
    PID 0x03, первый байт: 1 -- разомкнутый (ещё не готов), 2 --
    замкнутый по зонду, 4 -- разомкнутый по нагрузке, сбросу топлива или
    цели, отличной от 1.000, 8 -- разомкнутый из-за неисправности. 16 наш
    блок не шлёт никогда (andb #0xEF, 0x84B8C8).

    Осторожно: байт 0x8939 при части условий сохраняет прошлое значение
    (0x84B89E), и «2» может остаться, когда регулятор уже выключен. Точнее
    -- DID 1216 из режима 22.
    """
    return float(b[0])


def _trim_b(b) -> float | None:
    """
    PID 0x14, второй байт: STFT того же зонда. У нашего блока это та же
    ячейка 0x95AE, что и в PID 0x06 (0x864C72), -- поэтому и канал тот же.
    0xFF по стандарту -- «зонд в коррекции не участвует».
    """
    if len(b) < 2 or b[1] == 0xFF:
        return None
    return (b[1] - 128) * 100 / 128


def _maf_gs(b) -> float | None:
    """
    PID 0x10. Блок шлёт floor(ml / 9) × 25, ml -- целое в 0.1 кг/ч
    (0x864BEC): ответ меньше настоящего на 0…0.8 кг/ч, в среднем на 0.4
    (1/9 г/с). Эта середина и возвращается. 0xFFFF -- упор (ml ≥ 0x5C28),
    значения нет.
    """
    raw = _u16(b)
    if raw == 0xFFFF:
        return None
    return raw / 100.0 + 1.0 / 9.0


PIDS = {p.pid: p for p in (
    Pid(0x03, "Fuel system status", "", 2, _fuel_status,
        "контур топлива"),
    Pid(0x04, "Calculated engine load", "%", 1,
        lambda b: b[0] * 100 / 255, "расчётная нагрузка"),
    Pid(0x05, "Engine coolant temperature", "℃", 1,
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
    Pid(0x0F, "Intake air temperature", "℃", 1,
        lambda b: b[0] - 40.0, "температура воздуха"),
    Pid(0x10, "MAF air flow rate", "g/sec", 2, _maf_gs, "расход воздуха"),
    Pid(0x11, "Throttle position", "%", 1,
        lambda b: b[0] * 100 / 255, "дроссель"),
    # УДК -- родной узкий зонд. A -- байт 0x88CE (из АЦП 0x8901), по
    # стандарту 5 мВ на единицу; пересчёт делает функция сегмента 0, её в
    # дампе нет, так что масштаб -- по стандарту, не по коду. B -- STFT
    Pid(0x14, "Oxygen sensor 1 Bank 1 Voltage", "V", 2,
        lambda b: b[0] / 200.0, "УДК, узкий зонд",
        (("Short term fuel % trim - Bank 1", "%", _trim_b),)),
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


@dataclass(frozen=True)
class Mem:
    """
    Чтение памяти, KWP-сервис 0x23: запрос «23 AA AA AA LL», ответ
    «63 данные» (младший байт первым -- копия памяти C167).

    Есть только в заводской KWP-сессии (быстрая инициализация, протокол 5
    у ELM): обработчик 0x8382E8 проверяет лишь длину запроса и класс
    адреса, ключа не просит. Класс 1 -- внешнее ОЗУ 0x380000-0x387FFF, то
    есть данные 0x8000-0xBFFF через DPP2 = 0xE0. Пропустит ли запрос
    диспетчер сегмента 0, которого в дампе нет, -- проверяется на машине:
    логгер пробует при подключении.
    """
    addr: int
    name: str
    unit: str
    nbytes: int
    decode: Callable
    title: str = ""


def _le16(b) -> int:
    return b[0] | (b[1] << 8)


MEMS = {m.addr: m for m in (
    # заданная лямбда 0x9818: 0x1000 = 1.000 (docs/32, 0x84B948)
    Mem(0x381818, "Lambda target ECU", "λ", 2,
        lambda b: _le16(b) / 4096.0, "заданная λ"),
    # сырой АЦП ДМРВ 0x903C, то же, что DID 1235
    Mem(0x38103C, "MAF sensor ADC", "counts", 2,
        lambda b: float(_le16(b) & 0x3FF), "АЦП ДМРВ"),
)}


def encode23(addr: int, value: float) -> bytes:
    """Значение -> байты ответа сервиса 0x23 (для имитатора)."""
    if addr == 0x381818:
        v = int(round(value * 4096))
    elif addr == 0x38103C:
        v = int(round(value))
    else:
        raise KeyError("адрес %06X не описан" % addr)
    return max(0, min(0xFFFF, v)).to_bytes(2, "little")


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
#
# Без ШДК смесь считается по STFT -- его каждый круг. PID 0x14 отдаёт его
# вместе с напряжением УДК одним ответом: запрос тот же, а видно ещё, в
# упоре ли коррекция. Нет 0x14 -- спрашивается 0x06 (FALLBACK).
PLAN_01 = [
    ((1, 0x0C), 1),      # обороты
    ((1, 0x10), 1),      # расход, огрублённый до 0.25 г/с
    ((1, 0x11), 1),      # дроссель
    ((1, 0x14), 1),      # УДК и STFT
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
    ((1, 0x14), 1),       # УДК и STFT
    ((0x22, 0x1285), 2),  # расход, 0.1 кг/ч
    ((0x22, 0x1216), 4),  # регулятор по зонду
    ((1, 0x07), 8),
    ((1, 0x05), 20),
]
# KWP-сессия с чтением памяти: АЦП и заданная лямбда из ОЗУ, остальное
# -- режим 01 (режима 22 в этой сессии нет)
PLAN_MEM = [
    ((0x23, 0x38103C), 1),   # АЦП ДМРВ
    ((1, 0x0C), 1),
    ((1, 0x11), 1),
    ((1, 0x14), 1),          # УДК и STFT
    ((1, 0x10), 2),
    ((0x23, 0x381818), 2),   # заданная лямбда
    ((1, 0x03), 4),
    ((1, 0x07), 8),
    ((1, 0x05), 20),
]
DEFAULT_PLAN = PLAN_01
# не отвечает блок на первое -- спросить второе
FALLBACK = {(1, 0x14): (1, 0x06)}


def describe(key):
    """(режим, номер) -> описание величины: Pid или Did."""
    mode, ident = key
    if mode == 1:
        return PIDS.get(ident)
    if mode == 0x22:
        return DIDS.get(ident)
    return MEMS.get(ident)


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
        # как блок: ml в 0.1 кг/ч, floor(ml / 9) × 25
        ml10 = int(value * 3.6 * 10)
        raw = 0xFFFF if ml10 >= 0x5C28 else (ml10 // 9) * 25
        return raw.to_bytes(2, "big")
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
        # (вольты, STFT %) или одни вольты -- тогда «в коррекции не участвует»
        v, trim = value if isinstance(value, tuple) else (value, None)
        b = 0xFF if trim is None else clamp(trim * 128 / 100 + 128, 254)
        return bytes([clamp(v * 200), b])
    raise KeyError("PID 0x%02X не описан" % pid)
