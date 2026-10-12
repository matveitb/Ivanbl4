#!/usr/bin/env python3
"""
УДК по последовательному порту: разбор потока в лямбду.

Контроллер широкополосного датчика сам шлёт показания, спрашивать его не
надо. Разбор устроен одинаково для всех протоколов: в разборщик кормятся
байты как пришли, кусками любой длины, наружу выходят готовые показания.
Рассинхрон (подключились посреди пакета, потеряли байт) лечится поиском
следующего заголовка, а не падением и не выдумыванием.

Показание -- лямбда и состояние. Лямбда только тогда, когда контроллер
сам говорит, что она годна: датчик греется, калибровка в воздухе,
ошибка -- это не «бедная смесь», и в лог такое не пишется.

Протоколы (сверены с открытыми разборщиками: Tactrix innomts, RomRaider,
libISP2, rusEFI, txlogger -- docs/41):

* Innovate ISP2, 19200 8N1 -- LC-1, LC-2, LM-2, MTX-L, DLG-1, SSI-4 и
  почти все контроллеры, что продают у нас под ВАЗ (RUSC, MCE, самодельные
  «ШДК»): их делают «как LC-1», потому что только этот формат понимают
  программы вроде Online Tuner;
* текст, число в строке, 9600 8N1 -- AEM 30-4100/30-4110 и X-Series
  30-0300 (последовательный выход на синем проводе);
* PLX iMFD (SM-AFR, DM-6), 19200 8N1;
* Zeitronix ZT-2, 9600 8N1.

Не читаются: аналоговый выход 0-5 В (нужен АЦП), 14Point7 Spartan 3 (по
порту у него только команды, лямбда -- по CAN и аналогу), «быстрая»
прошивка LC-1 для «Матрицы» (115200, HDLC).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

STOICH = 14.7

OK = "ok"
WARMUP = "прогрев"
FREE_AIR = "калибровка в воздухе"
NEED_CAL = "нужна калибровка в воздухе"
HEATER_CAL = "калибровка нагревателя"
ERROR = "ошибка"
O2 = "кислород в воздухе"
RESERVED = "неизвестное состояние"


@dataclass
class Reading:
    lam: float | None       # лямбда, если годна
    status: str = OK
    raw: float | None = None   # что пришло: AFR, процент O2, код ошибки


class Parser:
    name = ""
    baud = 9600

    def feed(self, data: bytes) -> list[Reading]:
        raise NotImplementedError


# -- текст: число в строке -----------------------------------------------------

_FIRST_NUM = re.compile(r"^\s*([-+]?\d+(?:[.,]\d+)?)(?=\s|$|[;,\t])")


class AsciiParser(Parser):
    """
    Строка -- одно показание, и число должно стоять ПЕРВЫМ полем:
    «14.7», «1.000», «1.000<TAB>Ready<TAB>No-errors». Строка, которая
    начинается со слов («Board: VBatt 13.8 …»), -- не показание: иначе
    напряжение бортсети запишется как AFR.

    Если за числом идут поля состояния (AEM: «Ready», «No-errors»),
    они проверяются: не «Ready» -- датчик не готов, не «No-errors» --
    ошибка.

    AFR или лямбда -- по величине (больше 4 лямбды не бывает, меньше 4 AFR
    тоже), или как задано. Режим «процент O2» у AEM по числу от AFR не
    отличить -- его на приборе надо выключить.
    """
    name = "Текст: число в строке (AEM 30-4110, X-Series 30-0300)"
    baud = 9600

    def __init__(self, stoich: float = STOICH, kind: str = "auto"):
        self.stoich = stoich
        self.kind = kind        # "auto" | "lambda" | "afr"
        self.buf = b""

    def feed(self, data: bytes) -> list[Reading]:
        self.buf += data
        out = []
        while True:
            m = re.search(rb"[\r\n]", self.buf)
            if not m:
                break
            line = self.buf[:m.start()]
            self.buf = self.buf[m.end():]
            # двоичный мусор (выбран не тот протокол) -- не текст
            if any(b < 0x09 or 0x0D < b < 0x20 or b > 0x7E for b in line):
                continue
            r = self.line(line.decode("ascii"))
            if r is not None:
                out.append(r)
        if len(self.buf) > 256:            # мусор без переводов строки
            self.buf = self.buf[-64:]
        return out

    def line(self, s: str) -> Reading | None:
        m = _FIRST_NUM.match(s)
        if not m:
            return None
        v = float(m.group(1).replace(",", "."))
        fields = [f.strip() for f in re.split(r"\t", s) if f.strip()]
        if len(fields) >= 2 and fields[1].lower() != "ready":
            return Reading(None, WARMUP, v)
        if len(fields) >= 3 and fields[2].lower() not in ("no-errors",
                                                          "no errors"):
            return Reading(None, ERROR, v)
        kind = self.kind
        if kind == "auto":
            kind = "afr" if v > 4.0 else "lambda"
        lam = v / self.stoich if kind == "afr" else v
        if not (0.5 <= lam <= 2.5):
            return Reading(None, ERROR, v)
        return Reading(lam, OK, v)


# -- Innovate ISP2 ----------------------------------------------------------------

_ISP2_STATUS = {1: O2, 2: FREE_AIR, 3: NEED_CAL, 4: WARMUP, 5: HEATER_CAL,
                6: ERROR, 7: RESERVED}


class Isp2Parser(Parser):
    """
    Innovate Serial Protocol 2, 19200 8N1, пакет раз в 81.92 мс. Слова по
    16 бит, старший байт первым.

    Заголовок:  1 R 1 S C x 1 L8 | 1 L7..L1   L -- длина в словах, 9 бит
    LC-1/LC-2:  010F FF1A 0AAA AAAA          F -- состояние, A -- AFR ×10
                00LL LLLL 0LLL LLLL          L -- 13 бит
    LM-1 в цепочке: 100F FF0A 0AAA AAAA, лямбда, батарея, 5 входов -- 8 слов
    Вход (aux): 00AA AAAA 0AAA AAAA -- одно слово.

    Состояние: 000 -- лямбда годна, λ = (L + 500) / 1000; 001 -- кислород,
    L/10 %; 010 -- калибровка в воздухе; 011 -- нужна калибровка; 100 --
    прогрев, L/10 % рабочей температуры; 101 -- калибровка нагревателя;
    110 -- ошибка, код в L; 111 -- зарезервировано.

    В пакете может быть несколько датчиков: LM-2 на два канала, DLG-1,
    цепочка LC-1. Берётся датчик номер channel (по умолчанию первый), иначе
    два датчика перемешались бы в один канал.
    """
    name = "Innovate ISP2 (LC-1, LC-2, LM-2, MTX-L и «как LC-1»)"
    baud = 19200

    def __init__(self, channel: int = 0):
        self.buf = bytearray()
        self.channel = channel

    @staticmethod
    def _is_header(b0: int, b1: int) -> bool:
        return (b0 & 0xA2) == 0xA2 and (b1 & 0x80) == 0x80

    def feed(self, data: bytes) -> list[Reading]:
        self.buf += data
        out = []
        while len(self.buf) >= 2:
            if not self._is_header(self.buf[0], self.buf[1]):
                del self.buf[0]
                continue
            words = ((self.buf[0] & 0x01) << 7) | (self.buf[1] & 0x7F)
            need = 2 + 2 * words
            if words == 0:
                del self.buf[0]
                continue
            if len(self.buf) < need:
                break
            body = bytes(self.buf[2:need])
            del self.buf[:need]
            r = self._body(body)
            if r is not None:
                out.append(r)
        return out

    def _lambda(self, func: int, af10: int, L: int) -> Reading:
        if func == 0:
            if not 60 <= af10 <= 250:
                # стехиометрия от метанола (6.4) до 25 -- иначе слово битое
                return Reading(None, ERROR, float(af10))
            lam = (L + 500) / 1000.0
            return Reading(lam, OK, lam * af10 / 10.0)
        if func in (1, 4):
            return Reading(None, _ISP2_STATUS[func], L / 10.0)
        return Reading(None, _ISP2_STATUS[func], float(L))

    def _body(self, body: bytes) -> Reading | None:
        k = 0
        i = 0
        while i + 1 < len(body):
            b0, b1 = body[i], body[i + 1]
            if (b0 & 0xE2) == 0x42 and i + 3 < len(body):          # LC-1
                func = (b0 >> 2) & 0x07
                af10 = ((b0 & 0x01) << 7) | (b1 & 0x7F)
                L = ((body[i + 2] & 0x3F) << 7) | (body[i + 3] & 0x7F)
                if k == self.channel:
                    return self._lambda(func, af10, L)
                k += 1
                i += 4
            elif (b0 & 0xA2) == 0x80 and i + 15 < len(body):       # LM-1
                func = (b0 >> 2) & 0x07
                af10 = ((b0 & 0x01) << 7) | (b1 & 0x7F)
                L = ((body[i + 2] & 0x3F) << 7) | (body[i + 3] & 0x7F)
                if k == self.channel:
                    return self._lambda(func, af10, L)
                k += 1
                i += 16
            else:
                i += 2                     # слово вспомогательного входа
        return None


# -- PLX ------------------------------------------------------------------------------

class PlxParser(Parser):
    """
    PLX iMFD, 19200 8N1. Пакет: 0x80, затем по пять байт на датчик (адрес
    2 байта, номер 1, значение 2 -- по 6 бит в байте), в конце 0x40.
    Широкополосный датчик -- адрес 0, λ = (x / 3.75 + 68) / 100.

    Пакет, в котором после потери байта не кратно пяти, выбрасывается
    целиком: сдвинутые записи дают правдоподобную, но чужую лямбду. Если
    модулей два, берётся номер instance.
    """
    name = "PLX iMFD (SM-AFR, DM-6)"
    baud = 19200

    def __init__(self, instance: int = 0):
        self.buf = bytearray()
        self.instance = instance

    def feed(self, data: bytes) -> list[Reading]:
        self.buf += data
        out = []
        while True:
            try:
                e = self.buf.index(0x40)
            except ValueError:
                if len(self.buf) > 512:
                    del self.buf[:-64]
                break
            # начало -- последний 0x80 перед концом: потерянный 0x40
            # прошлого пакета не склеивает два пакета в один
            s = self.buf.rfind(0x80, 0, e)
            pkt = bytes(self.buf[s + 1:e]) if s >= 0 else b""
            del self.buf[:e + 1]
            if s < 0 or not pkt or len(pkt) % 5:
                continue
            for k in range(0, len(pkt), 5):
                rec = [b & 0x3F for b in pkt[k:k + 5]]
                addr = (rec[0] << 6) | rec[1]
                if addr != 0 or rec[2] != self.instance:
                    continue
                val = (rec[3] << 6) | rec[4]
                lam = (val / 3.75 + 68) / 100.0
                ok = 0.5 <= lam <= 2.5
                out.append(Reading(lam if ok else None, OK if ok else ERROR,
                                   float(val)))
        return out


# -- Zeitronix -----------------------------------------------------------------------

class ZeitronixParser(Parser):
    """
    Zeitronix ZT-2, 9600 8N1: пакеты по 14 байт, начало 00 01 02, четвёртый
    байт -- AFR бензина ×10 (λ = байт / 147).
    """
    name = "Zeitronix ZT-2"
    baud = 9600

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[Reading]:
        self.buf += data
        out = []
        while True:
            i = self.buf.find(b"\x00\x01\x02")
            if i < 0:
                del self.buf[:-2]
                break
            if len(self.buf) < i + 14:
                del self.buf[:i]
                break
            afr10 = self.buf[i + 3]
            del self.buf[:i + 14]
            lam = afr10 / 147.0
            ok = 0.5 <= lam <= 1.6
            out.append(Reading(lam if ok else None, OK if ok else ERROR,
                               afr10 / 10.0))
        return out


PARSERS = {
    "isp2": Isp2Parser,
    "ascii": AsciiParser,
    "plx": PlxParser,
    "zt2": ZeitronixParser,
}
PARSER_TITLES = [(k, PARSERS[k].name) for k in ("isp2", "ascii", "plx", "zt2")]


def make(kind: str, **kw) -> Parser:
    cls = PARSERS[kind]
    return cls(**kw) if kw and cls is AsciiParser else cls()


# -- кодирование (для имитатора и проверок) -----------------------------------------

def isp2_packet(lam: float | None, func: int = 0, afr_mult: float = 14.7,
                aux: int = 0, L: int | None = None, sensors: int = 1) -> bytes:
    """
    Пакет ISP2: sensors датчиков LC-1 (одинаковых) и aux входов. L --
    сырое значение для состояний, кроме 000 (процент O2, код ошибки...).
    """
    words = 2 * sensors + aux
    head = bytes([0xB2 | ((words >> 7) & 1), 0x80 | (words & 0x7F)])
    if L is None:
        L = (max(0, min(8191, int(round(lam * 1000 - 500))))
             if lam is not None and func == 0 else 0)
    a = int(round(afr_mult * 10))
    w0 = bytes([0x42 | (func << 2) | ((a >> 7) & 1), a & 0x7F])
    w1 = bytes([(L >> 7) & 0x3F, L & 0x7F])
    return head + (w0 + w1) * sensors + b"\x00\x00" * aux


def plx_packet(lam: float, instance: int = 0) -> bytes:
    """Пакет PLX с одним широкополосным датчиком."""
    x = max(0, min(4095, int(round((lam * 100 - 68) * 3.75))))
    return bytes([0x80, 0, 0, instance & 0x3F, (x >> 6) & 0x3F, x & 0x3F,
                  0x40])


def zt2_packet(lam: float) -> bytes:
    afr10 = max(0, min(255, int(round(lam * 147))))
    return bytes([0, 1, 2, afr10] + [0] * 10)
