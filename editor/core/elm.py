#!/usr/bin/env python3
"""
ELM327: разговор с блоком через адаптер.

Адаптер -- это текстовый терминал: пишем строку с переводом каретки,
он отвечает строками и в конце ставит приглашение «>». Всё остальное --
надстройка над этим.

Транспорт бывает трёх видов, и разговор от него не зависит:

* COM-порт -- USB-адаптер (CH340, FTDI) или Bluetooth: под Windows
  спаренный классический Bluetooth-адаптер виден как COM-порт;
* Wi-Fi -- TCP, обычно 192.168.0.10:35000;
* имитатор -- для проверок и демо-режима (fakecar.py).

Адаптеры только с Bluetooth Low Energy COM-портом не являются, и через
них ничего не выйдет -- это сказано в окне, а не оставлено на догадку.

Ответ разбирается терпимо: эхо, пробелы, «SEARCHING...», «BUS INIT»,
несколько строк, заголовки -- всё вычищается, остаются байты данных.
Ищется в них пара «41 PID», и берутся байты после неё.
"""

from __future__ import annotations

import re
import socket
import time
from dataclasses import dataclass, field

PROMPT = b">"
ERRORS = ("NO DATA", "UNABLE TO CONNECT", "BUS INIT: ...ERROR",
          "BUS INIT: ERROR", "BUS ERROR", "BUS BUSY", "CAN ERROR", "STOPPED",
          "ERROR", "BUFFER FULL", "FB ERROR", "DATA ERROR", "LV RESET", "?")
# после этих адаптер потерял настройки или шину -- нужна новая инициализация
FATAL = ("LV RESET", "UNABLE TO CONNECT", "BUS INIT: ...ERROR",
         "BUS INIT: ERROR")
ENGINE = 0x10                  # адрес блока двигателя на K-линии
NOISE = re.compile(r"SEARCHING\.*|BUS INIT:\s*\.*\s*OK|BUS INIT:?\s*\.*",
                   re.I)
HEX = re.compile(r"^[0-9A-Fa-f]+$")


class ElmError(Exception):
    pass


# -- транспорт ---------------------------------------------------------------

class Transport:
    """Байты туда и обратно. read возвращает то, что пришло, хоть пусто."""

    name = ""

    def write(self, data: bytes) -> None:
        raise NotImplementedError

    def read(self, timeout: float) -> bytes:
        raise NotImplementedError

    def flush_input(self) -> None:
        pass

    def close(self) -> None:
        pass


class SerialTransport(Transport):
    def __init__(self, port: str, baud: int = 38400):
        import serial                                       # pyserial
        self.name = "%s @ %d" % (port, baud)
        self.port = port
        self.s = serial.Serial(port, baud, timeout=0.02, write_timeout=1.0)

    def set_baud(self, baud: int) -> None:
        self.s.baudrate = baud
        self.name = "%s @ %d" % (self.port, baud)

    def write(self, data: bytes) -> None:
        self.s.write(data)
        self.s.flush()

    def read(self, timeout: float) -> bytes:
        self.s.timeout = max(0.001, timeout)
        n = self.s.in_waiting
        return self.s.read(n if n else 1) + (
            self.s.read(self.s.in_waiting) if self.s.in_waiting else b"")

    def flush_input(self) -> None:
        self.s.reset_input_buffer()

    def close(self) -> None:
        try:
            self.s.close()
        except Exception:                                   # noqa: BLE001
            pass


class TcpTransport(Transport):
    def __init__(self, host: str = "192.168.0.10", port: int = 35000):
        self.name = "%s:%d" % (host, port)
        self.sock = socket.create_connection((host, port), timeout=5.0)

    def write(self, data: bytes) -> None:
        self.sock.sendall(data)

    def read(self, timeout: float) -> bytes:
        self.sock.settimeout(max(0.001, timeout))
        try:
            return self.sock.recv(4096)
        except socket.timeout:
            return b""

    def flush_input(self) -> None:
        self.sock.setblocking(False)
        try:
            while self.sock.recv(4096):
                pass
        except (BlockingIOError, socket.error):
            pass
        finally:
            self.sock.setblocking(True)

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:                                   # noqa: BLE001
            pass


def list_serial_ports() -> list[tuple[str, str]]:
    """[(порт, описание)] -- что есть в системе. Без pyserial -- пусто."""
    try:
        from serial.tools import list_ports
    except Exception:                                       # noqa: BLE001
        return []
    out = []
    for p in sorted(list_ports.comports(), key=lambda p: p.device):
        out.append((p.device, p.description or ""))
    return out


# -- разбор ответа ------------------------------------------------------------

def clean_lines(text: str, cmd: str = "") -> list[str]:
    """Строки ответа без эха, приглашения и служебного шума."""
    out = []
    for raw in re.split(r"[\r\n]+", text.replace(">", "\r")):
        line = raw.strip()
        if not line:
            continue
        if cmd and line.replace(" ", "").upper() == cmd.replace(" ", "").upper():
            continue                                        # эхо
        line = NOISE.sub("", line).strip()
        if line:
            out.append(line)
    return out


def data_bytes(lines: list[str]) -> list[list[int]]:
    """Каждая строка с шестнадцатеричными байтами -> список байтов."""
    out = []
    for line in lines:
        s = line.replace(" ", "")
        if ":" in s:                       # «0: 41 00 ...» -- кадры CAN
            s = s.split(":", 1)[1]
        if HEX.match(s) and len(s) % 2 == 0:
            out.append([int(s[i:i + 2], 16) for i in range(0, len(s), 2)])
    return out


def frame(row: list[int]) -> tuple[int | None, list[int]]:
    """
    Разобрать строку с заголовком: (адрес отправителя, данные).

    KWP2000:   80+длина  F1  ECU  данные...  сумма   («84 F1 10 41 0C 1A F8 xx»)
               80        F1  ECU  длина  данные...  сумма  -- длинный ответ
    ISO 9141:  48  6B  ECU  данные...  сумма
    Если заголовка не видно (адаптер отдал их выключенными) -- вся строка
    данные, отправитель неизвестен.
    """
    if len(row) >= 4 and (row[0] & 0xC0) == 0x80 and row[1] == 0xF1:
        n = row[0] & 0x3F
        if n and len(row) == 3 + n + 1:
            return row[2], row[3:3 + n]
        if not n and len(row) >= 5 and len(row) == 4 + row[3] + 1:
            return row[2], row[4:4 + row[3]]
        # длина в заголовке не сходится со строкой (клон что-то склеил
        # или потерял) -- данные по факту: всё между заголовком и суммой
        return row[2], row[3:-1]
    if len(row) >= 4 and row[0] == 0x48 and row[1] == 0x6B:
        return row[2], row[3:-1]
    return None, row


def _ident(pid) -> list[int]:
    if pid is None:
        return []
    if isinstance(pid, int):
        return [pid]
    return list(pid)


def find_reply(rows: list[list[int]], mode: int, pid, n: int,
               src: int | None = None) -> list[int] | None:
    """
    Найти ответ «mode+0x40 PID данные». pid -- байт или список байтов
    (у режима 22 их два). Если строки с заголовками, а src задан, берётся
    только ответ этого блока: на функциональный запрос может ответить и
    коробка-автомат, и её обороты -- не обороты двигателя.
    """
    head = [mode + 0x40] + _ident(pid)
    for row in rows:
        who, data = frame(row)
        if who is not None:
            if src is not None and who != src:
                continue
            if data[:len(head)] == head and len(data) >= len(head) + n:
                return data[len(head):len(head) + n]
            continue
        for i in range(len(row) - len(head) + 1):
            if row[i:i + len(head)] == head:
                body = row[i + len(head):i + len(head) + n]
                if len(body) == n:
                    return body
    return None


def is_error(lines: list[str]) -> str:
    for line in lines:
        u = line.upper()
        for e in ERRORS:
            if u == e or u.startswith(e):
                return line
    return ""


# -- адаптер -------------------------------------------------------------------

@dataclass
class ElmStats:
    requests: int = 0
    answered: int = 0
    no_data: int = 0
    errors: int = 0
    last_error: str = ""
    fatal: str = ""            # ошибка, после которой нужна новая инициализация
    latency: float = 0.0       # с, скользящее среднее

    def note(self, dt: float) -> None:
        self.latency = dt if not self.latency else 0.9 * self.latency + 0.1 * dt


@dataclass
class Elm327:
    """
    Разговор с адаптером. Не потокобезопасен: им пользуется один поток
    опроса, и это сознательно -- ELM всё равно отвечает строго по одному.
    """
    t: Transport
    clock: object = time.monotonic
    version: str = ""
    protocol: str = ""
    stats: ElmStats = field(default_factory=ElmStats)
    count_suffix: bool = True     # «010C1»: ждать один ответ, а не таймаут
    log: list = field(default_factory=list)   # последние обмены для окна
    ecu: int | None = None        # адрес двигателя, если видны заголовки
    ecus: set = field(default_factory=set)    # кто ответил на 0100
    proto_req: str = "0"

    def command(self, cmd: str, timeout: float = 2.0) -> str:
        """Отправить строку, прочитать до приглашения. Текст ответа."""
        self.t.flush_input()
        self.t.write((cmd + "\r").encode("ascii"))
        buf = b""
        # таймауты -- по настоящим часам, даже если метки времени ставит
        # другая шкала (имитатор в проверках): иначе ожидание не кончится
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                break
            chunk = self.t.read(min(0.05, left))
            if chunk:
                buf += chunk
                if PROMPT in chunk:
                    break
        # у части PIC-клонов в потоке проскакивают нулевые байты
        text = buf.replace(b"\x00", b"").decode("ascii", "replace")
        self.log.append((cmd, text.strip()))
        del self.log[:-40]
        if PROMPT not in buf:
            raise ElmError("адаптер не ответил на «%s» за %.1f с" % (cmd, timeout))
        return text

    # -- подключение -------------------------------------------------------

    def init(self, protocol: str = "0", log=None) -> str:
        """
        Сброс и настройка. protocol -- номер для ATSP: «0» -- искать самому,
        «5» -- KWP2000 быстрый старт, «4» -- KWP 5 бод, «3» -- ISO 9141.
        """
        say = log or (lambda m: None)
        self.proto_req = protocol
        try:
            txt = self.command("ATZ", 4.0)
        except ElmError:
            # некоторые клоны после ATZ молчат или не печатают версию;
            # пустую строку слать нельзя -- ELM повторит прошлую команду
            txt = self.command("ATI", 2.0)
        m = re.search(r"ELM327\s*v?[\d.]+", txt, re.I)
        self.version = m.group(0) if m else txt.strip().replace("\r", " ")
        say("адаптер: " + (self.version or "?"))
        # Без пробелов -- на треть меньше байт на ответ, по Bluetooth это
        # заметно. Заголовки включены: по ним видно, какой блок ответил.
        for c in ("ATE0", "ATL0", "ATS0", "ATH1"):
            self.command(c, 2.0)
        self.command("ATAT1", 2.0)       # у части клонов «?» -- не страшно
        self.command("ATSP" + protocol, 2.0)
        # первый запрос будит K-линию: инициализация шины занимает секунды.
        # Без числа ответов: надо увидеть ВСЕ блоки, которые отзовутся
        say("связь с блоком...")
        txt = self.command("0100", 15.0)
        lines = clean_lines(txt, "0100")
        rows = data_bytes(lines)
        self.ecus = set()
        for row in rows:
            who, data = frame(row)
            if who is not None and data[:2] == [0x41, 0x00]:
                self.ecus.add(who)
        if self.ecus:
            self.ecu = ENGINE if ENGINE in self.ecus else min(self.ecus)
            if len(self.ecus) > 1:
                # ответят несколько -- «первый ответ» может оказаться
                # чужим; ждём всех и выбираем свой по адресу
                self.count_suffix = False
                say("на запрос отвечают блоки %s, беру %02X" % (
                    " ".join("%02X" % e for e in sorted(self.ecus)),
                    self.ecu))
        if find_reply(rows, 1, 0x00, 4, self.ecu) is None:
            err = is_error(lines) or " ".join(lines) or "пусто"
            raise ElmError("блок не ответил на 0100: " + err)
        try:
            self.protocol = clean_lines(self.command("ATDPN", 2.0),
                                        "ATDPN")[0]
        except (ElmError, IndexError):
            self.protocol = "?"
        say("протокол: %s" % self.protocol)
        return self.version

    def supported_pids(self) -> set:
        from obd import supported_from_bitmaps
        maps = {}
        for base in (0x00, 0x20):
            r = self.query(1, base, 4)
            if r is None:
                break
            maps[base] = r
            if not (r[3] & 1):         # PID base+0x20 не поддержан -- дальше нет
                break
        return supported_from_bitmaps(maps)

    # -- запросы -----------------------------------------------------------

    def query(self, mode: int, pid, n: int,
              timeout: float = 1.0) -> list[int] | None:
        """
        Один запрос режима mode. pid -- байт (режим 01) или список байтов
        (режим 22: [0x12, 0x35]). Байты данных или None.
        """
        cmd = "%02X" % mode + "".join("%02X" % b for b in _ident(pid))
        if self.count_suffix:
            cmd += "1"
        t0 = self.clock()
        self.stats.requests += 1
        try:
            txt = self.command(cmd, timeout)
        except ElmError as exc:
            self.stats.errors += 1
            self.stats.last_error = str(exc)
            return None
        lines = clean_lines(txt, cmd)
        rows = data_bytes(lines)
        body = find_reply(rows, mode, pid, n, self.ecu)
        if body is None:
            err = is_error(lines)
            if any(err.upper().startswith(f) for f in FATAL):
                self.stats.fatal = err
            if err.strip() == "?" and self.count_suffix:
                # клон не понимает число ответов в конце -- больше не шлём
                self.count_suffix = False
                return self.query(mode, pid, n, timeout)
            if err.upper().startswith("NO DATA"):
                self.stats.no_data += 1
            else:
                self.stats.errors += 1
                self.stats.last_error = err or " ".join(lines)
            return None
        self.stats.answered += 1
        self.stats.note(self.clock() - t0)
        return body

    def query22(self, did: int, n: int,
                timeout: float = 1.0) -> list[int] | None:
        """Режим 22 (данные по идентификатору): «22 12 35» -> «62 12 35 ..»."""
        return self.query(0x22, [did >> 8, did & 0xFF], n, timeout)

    def reinit(self, log=None) -> None:
        """Заново поднять адаптер и шину: после «LV RESET», смены зажигания."""
        self.stats.fatal = ""
        self.init(self.proto_req, log)

    def raw(self, hexstr: str, timeout: float = 1.0) -> list[list[int]]:
        """Произвольный запрос: строки байтов ответа, как есть."""
        txt = self.command(hexstr, timeout)
        return data_bytes(clean_lines(txt, hexstr))

    def close(self) -> None:
        self.t.close()


def connect_serial(port: str, baud: int | None = None,
                   log=None) -> SerialTransport:
    """
    Открыть COM-порт и подобрать скорость. У Bluetooth-порта скорость
    роли не играет, у USB-адаптеров бывает 38400 (обычно), 115200, 9600.
    """
    say = log or (lambda m: None)
    rates = [baud] if baud else [38400, 115200, 9600, 230400, 57600, 500000]
    last = None
    for b in rates:
        try:
            t = SerialTransport(port, b)
        except Exception as exc:                            # noqa: BLE001
            raise ElmError("порт %s не открылся: %s" % (port, exc))
        try:
            # мусорные байты, а не пустая строка: на пустую ELM повторяет
            # прошлую команду. Ответ «?» с приглашением -- значит, слышит
            t.flush_input()
            t.write(b"\x7F\x7F\r")
            buf = b""
            end = time.monotonic() + 1.0
            while time.monotonic() < end and PROMPT not in buf:
                buf += t.read(0.05)
            if PROMPT in buf:
                say("порт %s: адаптер отвечает на %d" % (port, b))
                return t
        except Exception as exc:                            # noqa: BLE001
            last = exc
        t.close()
    raise ElmError("на %s адаптер не ответил ни на одной скорости%s"
                   % (port, (": %s" % last) if last else ""))
