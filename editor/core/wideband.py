#!/usr/bin/env python3
"""
УДК по последовательному порту: разбор потока в лямбду.

Контроллер широкополосного датчика сам шлёт показания, спрашивать его не
надо. Разбор устроен одинаково для всех протоколов: в разборщик кормятся
байты как пришли, кусками любой длины, наружу выходят готовые показания.
Рассинхрон (подключились посреди пакета, потеряли байт) лечится поиском
следующего заголовка, а не падением.

Показание -- лямбда и состояние. Лямбда только тогда, когда контроллер
сам говорит, что она годна: датчик не прогрет, калибровка в воздухе,
ошибка -- это не «бедная смесь», и в лог такое не пишется.

Протоколы:

* Innovate ISP2 (LC-1, LC-2, LM-2, MTX-L, DLG-1, SSI-4) -- двоичный;
* AEM, 14Point7 Spartan и почти все прочие с последовательным выходом --
  текст, число в строке (AFR или лямбда, различается по величине);
* PLX (iMFD) -- двоичный, пакеты 0x80 ... 0x40;

Аналоговый выход 0-5 В так не прочитать: нужен АЦП, его в ноутбуке нет.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

STOICH = 14.7

OK = "ok"
WARMUP = "прогрев"
FREE_AIR = "калибровка в воздухе"
ERROR = "ошибка"
O2 = "кислород в воздухе"


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

_NUM = re.compile(r"[-+]?\d+(?:[.,]\d+)?")


class AsciiParser(Parser):
    """
    Строка -- одно показание. Берётся первое число: 14.7 -- это AFR,
    1.000 -- лямбда (больше 4 лямбды не бывает, меньше 4 AFR тоже).
    """
    name = "Текст: число в строке (AEM, Spartan и др.)"
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
            r = self.line(line.decode("latin-1", "replace"))
            if r is not None:
                out.append(r)
        if len(self.buf) > 256:            # мусор без переводов строки
            self.buf = self.buf[-64:]
        return out

    def line(self, s: str) -> Reading | None:
        m = _NUM.search(s)
        if not m:
            return None
        v = float(m.group(0).replace(",", "."))
        kind = self.kind
        if kind == "auto":
            kind = "afr" if v > 4.0 else "lambda"
        lam = v / self.stoich if kind == "afr" else v
        if not (0.5 <= lam <= 2.5):
            return Reading(None, ERROR, v)
        return Reading(lam, OK, v)


# -- Innovate ISP2 ----------------------------------------------------------------

class Isp2Parser(Parser):
    """
    Innovate Serial Protocol 2, 19200 8N1. Пакет -- 16-битные слова, старший
    байт первым.

    Заголовок:  1x11 xx1L 1LLL LLLL   L -- длина пакета в словах (без заголовка)
    LC-1/LC-2:  010F FF1A 0AAA AAAA   F -- состояние, A -- множитель AFR ×10
                00LL LLLL 0LLL LLLL   L -- 13 бит
    состояние: 000 -- лямбда годна, λ = (L + 500) / 1000
               001 -- кислород, % = L / 10
               010 -- калибровка в воздухе
               011 -- нужна калибровка
               100 -- прогрев
               101 -- калибровка нагревателя
               110 -- ошибка, код = L
    Вспомогательные каналы (аналоговые входы) -- по слову на канал, первые
    два бита 00 -- пропускаются.
    """
    name = "Innovate ISP2 (LC-1, LC-2, LM-2, MTX-L)"
    baud = 19200

    def __init__(self):
        self.buf = bytearray()

    @staticmethod
    def _is_header(b0: int, b1: int) -> bool:
        return (b0 & 0xB2) == 0xB2 and (b1 & 0x80) == 0x80

    def feed(self, data: bytes) -> list[Reading]:
        self.buf += data
        out = []
        while len(self.buf) >= 2:
            if not self._is_header(self.buf[0], self.buf[1]):
                del self.buf[0]
                continue
            words = ((self.buf[0] & 0x01) << 7) | (self.buf[1] & 0x7F)
            need = 2 + 2 * words
            if words == 0 or words > 64:
                del self.buf[0]
                continue
            if len(self.buf) < need:
                break
            body = bytes(self.buf[2:need])
            del self.buf[:need]
            out.extend(self._body(body))
        return out

    def _body(self, body: bytes) -> list[Reading]:
        out = []
        i = 0
        while i + 1 < len(body):
            b0, b1 = body[i], body[i + 1]
            if (b0 & 0xE2) == 0x42 and i + 3 < len(body):
                func = (b0 >> 2) & 0x07
                L = ((body[i + 2] & 0x3F) << 7) | (body[i + 3] & 0x7F)
                afr_mult = (((b0 & 0x01) << 7) | (b1 & 0x7F)) / 10.0
                if func == 0:
                    out.append(Reading((L + 500) / 1000.0, OK,
                                       (L + 500) / 1000.0 * afr_mult))
                elif func == 1:
                    out.append(Reading(None, O2, L / 10.0))
                elif func in (2, 3):
                    out.append(Reading(None, FREE_AIR, float(L)))
                elif func in (4, 5):
                    out.append(Reading(None, WARMUP, float(L)))
                else:
                    out.append(Reading(None, ERROR, float(L)))
                i += 4
            else:
                i += 2                     # слово вспомогательного канала
        return out


# -- PLX ------------------------------------------------------------------------------

class PlxParser(Parser):
    """
    PLX iMFD, 19200 8N1. Пакет: 0x80, затем по пять байт на датчик
    (адрес 2 байта, номер 1, значение 2 -- по 6 бит в байте), в конце 0x40.
    Широкополосный датчик -- адрес 0; значение 0..4095 -> лямбда
    = значение / 3.75 + 68 ... в шкале AFR; здесь берётся формула PLX для
    лямбды: λ = (значение / 3.75 + 68) / 100.
    """
    name = "PLX iMFD (SM-AFR)"
    baud = 19200

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[Reading]:
        self.buf += data
        out = []
        while True:
            try:
                s = self.buf.index(0x80)
            except ValueError:
                self.buf.clear()
                break
            try:
                e = self.buf.index(0x40, s + 1)
            except ValueError:
                if s:
                    del self.buf[:s]
                break
            pkt = bytes(self.buf[s + 1:e])
            del self.buf[:e + 1]
            for k in range(0, len(pkt) - 4, 5):
                addr = (pkt[k] << 6) | pkt[k + 1]
                val = (pkt[k + 3] << 6) | pkt[k + 4]
                if addr == 0:
                    lam = (val / 3.75 + 68) / 100.0
                    out.append(Reading(lam if 0.5 <= lam <= 2.5 else None,
                                       OK if 0.5 <= lam <= 2.5 else ERROR,
                                       float(val)))
        return out


PARSERS = {
    "isp2": Isp2Parser,
    "ascii": AsciiParser,
    "plx": PlxParser,
}
PARSER_TITLES = [(k, PARSERS[k].name) for k in ("isp2", "ascii", "plx")]


def make(kind: str, **kw) -> Parser:
    cls = PARSERS[kind]
    return cls(**kw) if kw and cls is AsciiParser else cls()


# -- кодирование (для имитатора и проверок) -----------------------------------------

def isp2_packet(lam: float | None, func: int = 0, afr_mult: float = 14.7,
                aux: int = 0) -> bytes:
    """Пакет ISP2 с одним LC-1 и aux вспомогательными словами."""
    words = 2 + aux
    head = bytes([0xB2 | ((words >> 7) & 1), 0x80 | (words & 0x7F)])
    if lam is not None and func == 0:
        L = max(0, min(8191, int(round(lam * 1000 - 500))))
    else:
        L = 0
    a = int(round(afr_mult * 10))
    w0 = bytes([0x42 | (func << 2) | ((a >> 7) & 1), a & 0x7F])
    w1 = bytes([(L >> 7) & 0x3F, L & 0x7F])
    return head + w0 + w1 + b"\x00\x00" * aux
