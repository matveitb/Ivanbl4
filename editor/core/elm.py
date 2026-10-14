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

Правила, которые здесь соблюдаются (ELM327 datasheet, ELM327DSK):

* писать только после «>»: любой байт, пришедший, пока адаптер занят,
  прерывает его -- в том числе его собственный поиск протокола;
* пустую строку не слать никогда: на неё ELM повторяет прошлую команду;
* «BUS INIT: ...ERROR», «UNABLE TO CONNECT», «LV RESET» -- адаптер
  потерял шину или настройки, нужна новая инициализация; они
  распознаются ДО того, как служебный текст вычищается из ответа;
* заголовки включены (ATH1): на функциональный запрос может ответить и
  коробка-автомат, ответ берётся только от адреса двигателя, а сумма в
  конце строки проверяется.
"""

from __future__ import annotations

import re
import socket
import time
from dataclasses import dataclass, field

PROMPT = b">"
ERRORS = ("NO DATA", "UNABLE TO CONNECT", "BUS INIT: ERROR", "BUS ERROR",
          "BUS BUSY", "CAN ERROR", "STOPPED", "BUFFER FULL", "FB ERROR",
          "DATA ERROR", "<DATA ERROR", "LV RESET", "ACT ALERT", "!ACT ALERT",
          "LP ALERT", "ERROR", "ERR", "?")
# после этих адаптер потерял настройки или шину -- нужна новая инициализация
FATAL = ("LV RESET", "UNABLE TO CONNECT", "BUS INIT: ERROR", "ERR",
         "LP ALERT", "ACT ALERT", "!ACT ALERT")
# Адрес двигателя в ответах. Наш блок, по прошивке, подписывается 0x17
# (0x8366C0, 0x836DFA), но бывают 0x10 и 0x11 -- поэтому двигатель
# выбирается не по адресу, а по смыслу: тот, кто объявляет расход воздуха.
ENGINES = (0x10, 0x11, 0x17)
HEX = re.compile(r"^[0-9A-Fa-f]+$")
_BUS_OK = re.compile(r"^BUS INIT:\s*\.*\s*OK$", re.I)
_BUS_BAD = re.compile(r"^BUS INIT:\s*\.*\s*(ERROR|UNABLE TO CONNECT)$", re.I)
_BUS_BARE = re.compile(r"^BUS INIT:\s*\.*$", re.I)
_SEARCH = re.compile(r"^SEARCHING\.*$", re.I)


class ElmError(Exception):
    pass


# -- транспорт ---------------------------------------------------------------

class Transport:
    """
    Байты туда и обратно. read возвращает то, что пришло, хоть пусто;
    обрыв связи -- исключение, а не пустота. flush_input возвращает то, что
    выбросил: среди мусора бывает «LV RESET», и его нельзя потерять.
    """

    name = ""

    def write(self, data: bytes) -> None:
        raise NotImplementedError

    def read(self, timeout: float) -> bytes:
        raise NotImplementedError

    def flush_input(self) -> bytes:
        return b""

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

    def flush_input(self) -> bytes:
        n = self.s.in_waiting
        return self.s.read(n) if n else b""

    def close(self) -> None:
        try:
            self.s.close()
        except Exception:                                   # noqa: BLE001
            pass


class TcpTransport(Transport):
    def __init__(self, host: str = "192.168.0.10", port: int = 35000):
        self.name = "%s:%d" % (host, port)
        self.sock = socket.create_connection((host, port), timeout=5.0)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def write(self, data: bytes) -> None:
        self.sock.sendall(data)

    def read(self, timeout: float) -> bytes:
        self.sock.settimeout(max(0.001, timeout))
        try:
            data = self.sock.recv(4096)
        except socket.timeout:
            return b""
        if not data:
            # recv вернул пусто без таймаута -- адаптер закрыл соединение
            raise ElmError("Wi-Fi адаптер закрыл соединение")
        return data

    def flush_input(self) -> bytes:
        out = b""
        self.sock.setblocking(False)
        try:
            while True:
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                out += chunk
        except (BlockingIOError, socket.error):
            pass
        finally:
            self.sock.setblocking(True)
        return out

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:                                   # noqa: BLE001
            pass


# -- какие порты есть и какой из них адаптер ------------------------------------

# Bluetooth-порт Windows: SPP-служба 1101 под BTHENUM (так pyserial отдаёт
# hwid -- путь к экземпляру устройства). В конце -- адрес устройства: у
# исходящего порта настоящий, у входящего -- нули, и счётчик после «_» у
# входящих бывает любой:
#   BTHENUM\{00001101-0000-1000-8000-00805F9B34FB}_LOCALMFG&0002\7&2B1A8F16&0&001DA5686E0D_C00000000
#   BTHENUM\{00001101-...}_VID&0001005D_PID&223B\8&2A5819FC&0&1C5216ACAD82_C00000000
#   BTHENUM\{00001101-...}_LOCALMFG&0000\7&2B1A8F16&0&000000000000_0000000D
# Служба обязательно 1101: pyserial перечисляет и модемы, и модем
# телефона по Bluetooth (служба 1103) с настоящим адресом за адаптер
# сойти не должен.
_BT = re.compile(r"BTHENUM", re.I)
_BT_SPP = re.compile(r"BTHENUM\\\{0*1101-0000-1000-8000-00805F9B34FB\}", re.I)
_BT_MAC = re.compile(r"&([0-9A-F]{12})_C?[0-9A-F]{8}$", re.I)
# так обычно зовутся ELM327 по Bluetooth
_ELM_NAME = re.compile(r"obd|elm|v-?link|vgate|konnwei|veepeak|icar|"
                       r"scan|car|diag|spp", re.I)


@dataclass
class PortInfo:
    device: str             # COM5
    description: str        # как Windows его называет
    kind: str = "other"     # bt-out | bt-in | bt | usb | other
    bt_name: str = ""       # имя спаренного устройства: OBDII, V-LINK...
    mac: str = ""

    @property
    def label(self) -> str:
        if self.kind == "bt-out":
            return "%s · Bluetooth%s · исходящий" % (
                self.device, " «%s»" % self.bt_name if self.bt_name else "")
        if self.kind == "bt-in":
            return "%s · Bluetooth · входящий — не этот" % self.device
        if self.kind == "bt":
            return "%s · Bluetooth" % self.device
        desc = re.sub(r"\s*\(%s\)\s*$" % re.escape(self.device), "",
                      self.description or "").strip()
        return "%s · %s" % (self.device, desc) if desc else self.device

    @property
    def rank(self) -> int:
        """Чем меньше, тем вероятнее адаптер: им список и начинается."""
        if self.kind == "bt-out":
            return 0 if _ELM_NAME.search(self.bt_name) else 1
        return {"bt": 2, "usb": 3, "other": 4, "bt-in": 9}.get(self.kind, 5)


def _bt_name_api(mac: str) -> str:
    """
    Имя устройства так, как его показывает Windows (с учётом
    переименования): BluetoothGetDeviceInfo из bthprops.cpl.
    """
    import ctypes
    from ctypes import wintypes

    class _ST(ctypes.Structure):
        _fields_ = [(n, wintypes.WORD) for n in (
            "y", "mo", "dow", "d", "h", "mi", "s", "ms")]

    class _Info(ctypes.Structure):          # BLUETOOTH_DEVICE_INFO
        _fields_ = [("dwSize", wintypes.DWORD),
                    ("Address", ctypes.c_ulonglong),
                    ("ulClassofDevice", wintypes.ULONG),
                    ("fConnected", wintypes.BOOL),
                    ("fRemembered", wintypes.BOOL),
                    ("fAuthenticated", wintypes.BOOL),
                    ("stLastSeen", _ST), ("stLastUsed", _ST),
                    ("szName", wintypes.WCHAR * 248)]
    try:
        api = ctypes.WinDLL("bthprops.cpl")
    except (OSError, AttributeError):
        return ""
    info = _Info()
    info.dwSize = ctypes.sizeof(info)
    info.Address = int(mac, 16)
    if api.BluetoothGetDeviceInfo(None, ctypes.byref(info)) != 0:
        return ""
    return (info.szName or "").strip()


def _bt_name_reg(mac: str) -> str:
    """
    То же из реестра: FriendlyName -- если переименовали, Name -- как
    устройство назвалось само. Двоичные строки UTF-8 с нулём в конце.
    Читается без прав администратора.
    """
    try:
        import winreg
    except ImportError:
        return ""
    key = (r"SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters"
           r"\Devices\%s" % mac.lower())
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
            for value in ("FriendlyName", "Name"):
                try:
                    v, _ = winreg.QueryValueEx(k, value)
                except OSError:
                    continue
                if isinstance(v, (bytes, bytearray)):
                    v = bytes(v).split(b"\0")[0].decode("utf-8", "replace")
                if str(v).strip():
                    return str(v).strip()
    except OSError:
        pass
    return ""


def _bt_name(mac: str) -> str:
    """
    Имя спаренного Bluetooth-устройства по адресу -- то, что Windows
    показывает в «Bluetooth и устройства». Не Windows -- пусто.
    """
    for f in (_bt_name_api, _bt_name_reg):
        try:
            name = f(mac)
        except Exception:                                   # noqa: BLE001
            name = ""
        if name:
            return name
    return ""


def classify_port(device: str, description: str = "", hwid: str = "",
                  name_of=_bt_name) -> PortInfo:
    p = PortInfo(device, "" if description in (None, "n/a") else description)
    hw = hwid or ""
    if _BT.search(hw):
        m = _BT_MAC.search(hw)
        if m and int(m.group(1), 16) == 0:
            p.kind = "bt-in"
        elif m and _BT_SPP.search(hw):
            p.kind = "bt-out"
            p.mac = m.group(1).upper()
            p.bt_name = name_of(p.mac)
        else:
            p.kind = "bt"
    elif re.search(r"bluetooth", p.description, re.I):
        p.kind = "bt"
    elif hw.upper().startswith(("USB", "FTDIBUS")) or re.search(
            r"usb|ch34\d|ftdi|cp210|pl2303", p.description, re.I):
        p.kind = "usb"
    return p


def port_infos() -> list[PortInfo]:
    """Порты системы, адаптер вероятнее всего -- первым. Без pyserial -- []."""
    try:
        from serial.tools import list_ports
    except Exception:                                       # noqa: BLE001
        return []
    out = [classify_port(p.device, p.description or "", p.hwid or "")
           for p in list_ports.comports()]
    return sorted(out, key=lambda p: (p.rank, len(p.device), p.device))


def list_serial_ports() -> list[tuple[str, str]]:
    """[(порт, подпись)] -- что есть в системе. Без pyserial -- пусто."""
    return [(p.device, p.label) for p in port_infos()]


def run_within(fn, limit: float):
    """
    fn() в отдельном потоке, не дольше limit секунд. Открытие
    Bluetooth-порта Windows бывает зависает насовсем (адаптер включён, но не
    отвечает): ждать его вечно нельзя. Не успело -- TimeoutError, а если
    порт всё-таки откроется позже, он тут же закрывается.
    """
    import threading
    box = {}
    late = threading.Event()

    def run():
        try:
            box["v"] = fn()
        except BaseException as exc:                        # noqa: BLE001
            box["e"] = exc
        if late.is_set() and "v" in box:
            try:
                box["v"].close()
            except Exception:                               # noqa: BLE001
                pass

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(limit)
    if th.is_alive():
        late.set()
        raise TimeoutError("не открылся за %g с" % limit)
    if "e" in box:
        raise box["e"]
    return box["v"]


def find_elm(ports: list[PortInfo], opener=None, log=None,
             cancelled=lambda: False, limit: float = 20.0) -> str | None:
    """
    Перебрать порты и найти тот, где отвечает ELM327: ATI -> «ELM327».
    Входящие Bluetooth-порты не трогаются -- адаптер на них не ответит.
    Открытие исходящего порта само поднимает связь с адаптером: обычно
    секунда-две; если адаптер выключен, Windows ждёт 5-13 с и отвечает
    «превышен таймаут семафора», а бывает, что и не отвечает вовсе --
    поэтому на порт не больше limit секунд, и сначала самые вероятные.
    """
    say = log or (lambda m: None)
    opener = opener or (lambda dev: connect_serial(dev, None, say))
    for p in sorted(ports, key=lambda p: p.rank):
        if cancelled():
            return None
        if p.kind == "bt-in":
            continue
        say("пробую %s..." % p.label)
        try:
            t = run_within(lambda: opener(p.device), limit)
        except Exception as exc:                            # noqa: BLE001
            say("  %s: %s" % (p.device, exc))
            continue
        try:
            txt = Elm327(t).command("ATI", 2.0)
        except Exception as exc:                            # noqa: BLE001
            txt = ""
            say("  %s: %s" % (p.device, exc))
        finally:
            t.close()
        ans = " ".join(clean_lines(txt, "ATI"))
        if re.search(r"ELM\s*32[37]", ans, re.I):
            say("адаптер на %s: %s" % (p.device, ans))
            return p.device
        say("  %s: отвечает, но не ELM327 (%r)" % (p.device, ans[:30]))
    return None


# -- разбор ответа ------------------------------------------------------------

def clean_lines(text: str, cmd: str = "") -> list[str]:
    """
    Строки ответа без эха, приглашения и служебного шума.

    «SEARCHING...» и «BUS INIT: OK» выбрасываются. Неудача инициализации
    шины, наоборот, сводится к одной строке «BUS INIT: ERROR» -- в каком бы
    виде её ни напечатал адаптер: одной строкой, с точками или без, или
    «BUS INIT: ...» и «ERROR» следующей строкой.
    """
    out = []
    bare = False
    for raw in re.split(r"[\r\n]+", text.replace(">", "\r")):
        line = raw.strip()
        if not line:
            continue
        if cmd and line.replace(" ", "").upper() == cmd.replace(" ", "").upper():
            continue                                        # эхо
        if _SEARCH.match(line) or _BUS_OK.match(line):
            bare = False
            continue
        if _BUS_BAD.match(line):
            out.append("BUS INIT: ERROR")
            bare = False
            continue
        if _BUS_BARE.match(line):
            bare = True
            continue
        if bare:
            bare = False
            u = line.upper()
            if u in ("ERROR", "UNABLE TO CONNECT"):
                out.append("BUS INIT: ERROR")
                continue
            if u == "OK":
                continue
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


def checksum_ok(row: list[int]) -> bool:
    """Последний байт строки с заголовком -- сумма всех предыдущих."""
    return len(row) >= 2 and (sum(row[:-1]) & 0xFF) == row[-1]


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
    коробка-автомат, и её обороты -- не обороты двигателя. Строка с
    заголовком и неверной суммой отбрасывается.
    """
    head = [mode + 0x40] + _ident(pid)
    for row in rows:
        who, data = frame(row)
        if who is not None:
            if src is not None and who != src:
                continue
            if not checksum_ok(row):
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
    """Первая строка-ошибка адаптера (в порядке ERRORS -- длинные раньше)."""
    for line in lines:
        u = line.upper()
        for e in ERRORS:
            if u == e or u.startswith(e):
                if e == "ERR" and not re.match(r"^ERR\d", u):
                    continue
                return line
    return ""


def is_fatal(err: str) -> bool:
    u = err.upper()
    return any(u == f or u.startswith(f) for f in FATAL) and not (
        u.startswith("ERR") and not re.match(r"^ERR\d", u))


# -- адаптер -------------------------------------------------------------------

@dataclass
class ElmStats:
    requests: int = 0
    answered: int = 0
    no_data: int = 0
    errors: int = 0
    last_error: str = ""
    fatal: str = ""            # ошибка, после которой нужна новая инициализация
    # запросов подряд, на которые адаптер не ответил ничем, даже «>»: так
    # пропадает Bluetooth -- порт Windows остаётся открытым, а ответов нет
    silent: int = 0
    latency: float = 0.0       # с, скользящее среднее
    reinits: int = 0

    def note(self, dt: float) -> None:
        self.latency = dt if not self.latency else 0.9 * self.latency + 0.1 * dt


@dataclass
class Elm327:
    """
    Разговор с адаптером. Не потокобезопасен: им пользуется один поток
    опроса, и это сознательно -- ELM всё равно отвечает строго по одному.

    reopen -- как открыть транспорт заново (после обрыва Bluetooth или
    Wi-Fi старый уже мёртв). Без него переподключение работает только на
    том же транспорте.
    """
    t: Transport
    clock: object = time.monotonic
    reopen: object = None
    version: str = ""
    protocol: str = ""
    stats: ElmStats = field(default_factory=ElmStats)
    count_suffix: bool = True     # «010C1»: ждать один ответ, а не таймаут
    log: list = field(default_factory=list)   # последние обмены для окна
    ecu: int | None = None        # адрес двигателя, если видны заголовки
    ecus: set = field(default_factory=set)    # кто ответил на 0100
    proto_req: str = "0"
    proto_user: str = "0"         # что выбрал человек: «0» -- сам
    physical: bool = False        # запросы адресуются двигателю, а не всем

    # -- обмен -----------------------------------------------------------------

    def command(self, cmd: str, timeout: float = 2.0) -> str:
        """
        Отправить строку, прочитать до приглашения. Текст ответа.

        Если адаптер пишет «SEARCHING...» или «BUS INIT», он занят своей
        инициализацией шины -- ждём дольше, а не обрываем его следующей
        командой. Если приглашения так и нет, ждём ещё немного, потом
        прерываем адаптер одним байтом и дочитываем до «>»: следующая
        команда должна начаться с чистого листа, а не получить чужой
        запоздалый ответ.
        """
        try:
            junk = self.t.flush_input()
            if junk and (b"LV RESET" in junk or b"ELM327" in junk):
                self.stats.fatal = "LV RESET"
            self.t.write((cmd + "\r").encode("ascii"))
            buf = b""
            # таймауты -- по настоящим часам, даже если метки времени
            # ставит другая шкала (имитатор в проверках)
            start = time.monotonic()
            end = start + timeout
            while time.monotonic() < end:
                chunk = self.t.read(min(0.05, max(0.001, end - time.monotonic())))
                if chunk:
                    buf += chunk
                    if PROMPT in chunk:
                        break
                    if b"SEARCHING" in buf or b"BUS INIT" in buf:
                        end = max(end, start + 20.0)
            if PROMPT not in buf:
                buf += self._resync()
        except ElmError:
            raise
        except Exception as exc:                            # noqa: BLE001
            # pyserial, сокет: обрыв Bluetooth, выдернутый USB, Wi-Fi
            raise ElmError("связь с адаптером оборвалась: %s" % exc)
        # у части PIC-клонов в потоке проскакивают нулевые байты
        text = buf.replace(b"\x00", b"").decode("ascii", "replace")
        self.log.append((cmd, text.strip()))
        del self.log[:-40]
        if "LV RESET" in text.upper():
            self.stats.fatal = "LV RESET"
        if PROMPT not in buf:
            raise ElmError("адаптер не ответил на «%s» за %.1f с" % (cmd, timeout))
        return text

    def _resync(self) -> bytes:
        """Дождаться «>», при необходимости прервав адаптер одним байтом."""
        buf = b""
        end = time.monotonic() + 2.0
        while time.monotonic() < end and PROMPT not in buf:
            buf += self.t.read(0.05)
        if PROMPT in buf:
            return buf
        # байт-прерыватель: управляющие символы адаптер в команду не берёт
        self.t.write(b"\x7F")
        end = time.monotonic() + 2.0
        while time.monotonic() < end and PROMPT not in buf:
            buf += self.t.read(0.05)
        return buf

    # -- подключение -------------------------------------------------------

    def init(self, protocol: str = "0", log=None) -> str:
        """
        Сброс и настройка. protocol -- номер для ATSP: «0» -- искать самому,
        «5» -- KWP2000 быстрый старт, «4» -- KWP 5 бод, «3» -- ISO 9141.
        Найденный протокол запоминается: при переподключении он пробуется
        первым (ATSP A5), и полный поиск не нужен.
        """
        say = log or (lambda m: None)
        if not protocol.startswith("A"):
            self.proto_user = protocol
        self.proto_req = protocol
        self.count_suffix = True
        self.ecu = None
        self.ecus = set()
        self.physical = False
        self.stats.fatal = ""
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
        for c in ("ATE0", "ATL0", "ATS0"):
            self.command(c, 2.0)
        if "OK" not in self.command("ATH1", 2.0).upper():
            say("адаптер не включил заголовки -- блоки не различить")
        self.command("ATAT1", 2.0)       # у части клонов «?» -- не страшно
        self.command("ATSP" + protocol, 2.0)
        # первый запрос будит K-линию: инициализация шины занимает секунды.
        # Без числа ответов: надо увидеть ВСЕ блоки, которые отзовутся
        say("связь с блоком...")
        txt = self.command("0100", 20.0)
        lines = clean_lines(txt, "0100")
        rows = data_bytes(lines)
        from obd import supported_from_bitmaps
        maps = {}
        for row in rows:
            who, data = frame(row)
            if (who is not None and data[:2] == [0x41, 0x00]
                    and len(data) >= 6 and checksum_ok(row)):
                maps[who] = data[2:6]
        self.ecus = set(maps)
        if self.ecus:
            # двигатель -- кто объявляет расход воздуха (PID 0x10); у
            # коробки его нет
            maf = sorted(w for w, b in maps.items()
                         if 0x10 in supported_from_bitmaps({0: b}))
            known = [w for w in maf if w in ENGINES]
            self.ecu = (known or maf or
                        [w for w in sorted(self.ecus) if w in ENGINES] or
                        sorted(self.ecus))[0]
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
        digit = self.protocol[-1:] if self.protocol else ""
        if protocol == "0" and digit in "345" and digit:
            # дальше -- сначала найденный, поиск только если он не пойдёт
            self.proto_req = "A" + digit
        return self.version

    def use_physical(self, on: bool) -> bool:
        """
        Адресовать запросы двигателю напрямую (KWP: ATSH 81 <ECU> F1) или
        всем (C1 33 F1). Нужен, если режим 22 блок отдаёт только по своему
        адресу. Возвращает, получилось ли.
        """
        if not self.protocol or self.protocol[-1:] not in "45" or self.ecu is None:
            return False
        hdr = ("81 %02X F1" % self.ecu) if on else "C1 33 F1"
        ok = "OK" in self.command("ATSH " + hdr, 2.0).upper()
        self.command("ATWM %s 3E" % hdr, 2.0)
        self.physical = on and ok
        return ok

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

    def query(self, mode: int, pid, n: int, timeout: float = 1.0,
              echo=None) -> list[int] | None:
        """
        Один запрос режима mode. pid -- байт (режим 01) или список байтов
        (режим 22: [0x12, 0x35]). echo -- что блок повторяет в ответе
        после mode+0x40; по умолчанию то же, что спросили (у 0x23 -- ничего).
        Байты данных или None.
        """
        cmd = "%02X" % mode + "".join("%02X" % b for b in _ident(pid))
        reply_id = _ident(pid) if echo is None else list(echo)
        if self.count_suffix:
            cmd += "1"
        t0 = self.clock()
        self.stats.requests += 1
        try:
            txt = self.command(cmd, timeout)
        except ElmError as exc:
            self.stats.errors += 1
            self.stats.last_error = str(exc)
            if "не ответил" in str(exc):
                self.stats.silent += 1
            if "оборвалась" in str(exc) or "закрыл" in str(exc):
                self.stats.fatal = str(exc)
            return None
        self.stats.silent = 0
        lines = clean_lines(txt, cmd)
        rows = data_bytes(lines)
        body = find_reply(rows, mode, reply_id, n, self.ecu)
        if (body is not None and self.ecu is not None and rows
                and all(frame(r)[0] is None for r in rows)):
            # заголовки были, а теперь нет -- адаптер сбросился (LV RESET
            # без единого слова), и адрес блока больше не проверяется
            self.stats.fatal = "адаптер сбросил настройки"
        if body is None:
            err = is_error(lines)
            if is_fatal(err):
                self.stats.fatal = err
            if err.strip() == "?" and self.count_suffix:
                # клон не понимает число ответов в конце -- больше не шлём
                self.count_suffix = False
                return self.query(mode, pid, n, timeout, echo)
            neg = [r for r in (frame(x)[1] for x in rows)
                   if len(r) >= 3 and r[0] == 0x7F and r[1] == mode]
            if neg:
                # отрицательный ответ блока: сервис или величина не даны
                self.stats.errors += 1
                self.stats.last_error = "отказ блока: 7F %02X %02X" % (
                    mode, neg[0][2])
                return None
            if err.upper().startswith("NO DATA"):
                self.stats.no_data += 1
            else:
                self.stats.errors += 1
                self.stats.last_error = err or " ".join(lines) or "пусто"
            return None
        self.stats.answered += 1
        self.stats.note(self.clock() - t0)
        return body

    def query22(self, did: int, n: int,
                timeout: float = 1.0) -> list[int] | None:
        """Режим 22 (данные по идентификатору): «22 12 35» -> «62 12 35 ..»."""
        return self.query(0x22, [did >> 8, did & 0xFF], n, timeout)

    def query23(self, addr: int, n: int,
                timeout: float = 1.0) -> list[int] | None:
        """Чтение памяти: «23 AA AA AA LL» -> «63 данные»."""
        return self.query(0x23, [(addr >> 16) & 0xFF, (addr >> 8) & 0xFF,
                                 addr & 0xFF, n], n, timeout, echo=[])

    def reinit(self, log=None) -> None:
        """
        Заново поднять адаптер и шину: после «LV RESET», выключенного
        зажигания, обрыва Bluetooth или Wi-Fi. Если транспорт умеет
        открываться заново -- открывается заново.
        """
        self.stats.reinits += 1
        self.stats.fatal = ""
        physical = self.physical
        if self.reopen is not None:
            try:
                self.t.close()
            except Exception:                               # noqa: BLE001
                pass
            self.t = self.reopen()
        self.init(self.proto_req, log)
        if physical:
            self.use_physical(True)

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
    Порт открывается один раз, скорость меняется на открытом: Bluetooth
    при каждом открытии заново поднимает радиоканал, а это секунды.
    """
    say = log or (lambda m: None)
    rates = [baud] if baud else [38400, 115200, 9600, 230400, 57600, 500000]
    try:
        t = SerialTransport(port, rates[0])
    except Exception as exc:                                # noqa: BLE001
        raise ElmError("порт %s не открылся: %s" % (port, exc))
    last = None
    for b in rates:
        try:
            t.set_baud(b)
            time.sleep(0.05)
            t.flush_input()
            # мусорные байты, а не пустая строка: на пустую ELM повторяет
            # прошлую команду. Ответ «?» с приглашением -- значит, слышит
            t.write(b"\x7F\x7F\r")
            buf = b""
            end = time.monotonic() + 1.0
            while time.monotonic() < end and PROMPT not in buf:
                buf += t.read(0.05)
            # «>» должен быть последним: на чужой скорости в мусоре он
            # попадается случайно
            if buf.rstrip().endswith(PROMPT):
                say("порт %s: адаптер отвечает на %d" % (port, b))
                return t
        except Exception as exc:                            # noqa: BLE001
            last = exc
    t.close()
    raise ElmError("на %s адаптер не ответил ни на одной скорости%s"
                   % (port, (": %s" % last) if last else ""))
