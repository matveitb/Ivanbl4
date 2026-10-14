#!/usr/bin/env python3
"""
Логгер: блок через ELM, и ШДК через свой порт, если он есть, -- в один
файл, на одних часах.

Главное, ради чего он нужен, -- смесь по УДК, родному узкому зонду. Тогда
ШДК не нужен вовсе: ошибка ДМРВ видна в коррекциях, и логгер спрашивает
их каждый круг. PID 0x14 отдаёт напряжение УДК и STFT одним ответом, так
что видно и то, упёрлась ли коррекция. Car Scanner делит запросы поровну
между всем, что выбрано; здесь -- по нужде.

Если ШДК есть, его лог пишется тем же процессом по тем же часам, и
сводить нечего: остаётся только задержка самого датчика, она задаётся
при расчёте.

Как устроено:

* поток ELM -- спрашивает PID по плану: обороты, расход, дроссель, УДК со
  STFT каждый круг, LTFT и ОЖ редко. По K-линии на всё про всё около
  десяти запросов в секунду, и делить их поровну с температурой ОЖ --
  расточительство;
* поток ШДК -- читает порт, разбирает протокол, пишет годную лямбду;
* запись -- сразу в файл, построчно, в том же формате, что у Car Scanner
  ("SECONDS";"PID";"VALUE";"UNITS"). Поэтому лог читается тем же
  разбором, падение программы не теряет снятое, а метка времени у
  каждого показания своя -- без выдуманного выравнивания.

Метка показания ELM -- середина между запросом и ответом: блок отдаёт
значение где-то в этом промежутке, середина ошибается меньше всего.
"""

from __future__ import annotations

import os
import random
import threading
import time
from dataclasses import dataclass, field

import logio
import obd

# ШДК, если подключён. УДК -- родной узкий зонд -- приходит от блока, PID 0x14
LAMBDA_NAME = "ШДК лямбда"
LAMBDA_UNIT = "λ"


class Recorder:
    """Каналы в памяти плюс файл, дописываемый по строке. Потокобезопасен."""

    def __init__(self, path: str = ""):
        self.lock = threading.Lock()
        self.channels: dict = {}
        self.latest: dict = {}          # имя -> (время, значение)
        self.path = path
        self.fh = None
        self._dirty = 0
        if path:
            d = os.path.dirname(os.path.abspath(path))
            os.makedirs(d, exist_ok=True)
            self.fh = open(path, "w", encoding="utf-8", newline="")
            self.fh.write('"SECONDS";"PID";"VALUE";"UNITS"\r\n')
            self.fh.flush()

    def add(self, name: str, unit: str, t: float, v: float) -> None:
        with self.lock:
            ch = self.channels.get(name)
            if ch is None:
                ch = self.channels[name] = logio.Channel(name, unit)
            if ch.t and t < ch.t[-1]:
                t = ch.t[-1]            # часы одни, но порядок -- святое
            ch.t.append(t)
            ch.v.append(v)
            self.latest[name] = (t, v)
            if self.fh is not None:
                self.fh.write('"%.3f";"%s";"%s";"%s"\r\n'
                              % (t, name, ("%.4f" % v).rstrip("0").rstrip("."),
                                 unit))
                self._dirty += 1
                if self._dirty >= 20:
                    self.fh.flush()
                    self._dirty = 0

    def snapshot(self) -> logio.LogFile:
        """Копия для расчёта: поток записи при этом не стоит."""
        with self.lock:
            log = logio.LogFile(path=self.path, fmt="long")
            for name, ch in self.channels.items():
                log.channels[name] = logio.Channel(ch.name, ch.unit,
                                                   list(ch.t), list(ch.v))
        return log

    def close(self) -> None:
        with self.lock:
            if self.fh is not None:
                self.fh.flush()
                self.fh.close()
                self.fh = None


def probe_dids(elm) -> set:
    """
    Какие величины режима 22 блок отдаёт на этой машине. По коду они есть
    (docs/41), но достаётся ли режим 22 через ELM -- проверяется здесь:
    не ответил -- в план не попадёт.
    """
    out = set()
    for did, d in obd.DIDS.items():
        if elm.query22(did, d.nbytes) is not None:
            out.add(did)
    return out


def probe_mem(elm) -> set:
    """
    Чтение памяти (KWP 0x23): какие адреса блок отдаёт. Есть только в
    заводской KWP-сессии; по коду ключа не просит, но диспетчер сегмента 0
    в дампе не виден -- поэтому проба. Не ответил -- не спрашиваем.
    """
    out = set()
    for addr, m in obd.MEMS.items():
        if elm.query23(addr, m.nbytes) is not None:
            out.add(addr)
    return out


def plan_for(dids: set, mems: set | None = None, wb: bool = False) -> list:
    """
    План по тому, что умеет блок. С ШДК смесь меряет он, и УДК со STFT
    спрашиваются через круг -- чаще достаются обороты, дроссель и АЦП:
    по ним лямбда раскладывается по узлам. Без ШДК STFT -- это и есть
    измерение, его каждый круг.
    """
    if mems and 0x38103C in mems:
        plan = obd.PLAN_MEM
    else:
        plan = obd.PLAN_22 if 0x1235 in dids else obd.PLAN_01
    if wb:
        plan = [(k, 2 if k == (1, 0x14) else w) for k, w in plan]
    return plan


def prepare(elm, say=None) -> tuple[set, set, set]:
    """
    После init: что блок отдаёт и каким путём его снимать.

    У блока две двери на K-линии (docs/41). Медленная инициализация (ISO
    9141-2, протокол 3) открывает OBD-сессию: там режим 22 с сырым АЦП
    ДМРВ. Быстрая (KWP2000, протокол 5) -- заводскую: режима 22 нет, но
    есть чтение памяти, а через него и АЦП, и заданная лямбда. Если
    человек выбрал «сам» и ELM попал в KWP, где память не читается, --
    переподключаемся по ISO 9141 ради режима 22.

    Возвращает (PID режима 01, DID режима 22, адреса памяти).
    """
    say = say or (lambda m: None)
    sup = elm.supported_pids()
    dids = probe_dids(elm)
    mems = set()
    kwp = elm.protocol[-1:] in ("4", "5")
    if not dids and kwp:
        mems = probe_mem(elm)
        if mems:
            say("чтение памяти работает: %s" % ", ".join(
                obd.MEMS[a].title for a in sorted(mems)))
        elif elm.proto_user == "0":
            say("режим 22 есть только после медленной инициализации -- "
                "переподключаюсь по ISO 9141")
            try:
                _switch_to_iso(elm, say)
                sup = elm.supported_pids()
                dids = probe_dids(elm)
            except Exception as exc:                        # noqa: BLE001
                say("ISO 9141 не вышло (%s), остаюсь на KWP" % exc)
                elm.init("5", say)
                sup = elm.supported_pids()
    return sup, dids, mems


def _switch_to_iso(elm, say) -> None:
    """
    Перейти с KWP на ISO 9141. Блок держит KWP-сессию до 5 с тишины
    (P3max), и медленная инициализация до этого не пройдёт: если первая
    попытка не удалась, ждём и пробуем ещё раз.
    """
    try:
        elm.init("3", say)
    except Exception:                                       # noqa: BLE001
        time.sleep(5.5)
        elm.init("3", say)


def schedule(plan, supported: set | None, dids: set | None = None,
             mems: set | None = None) -> list:
    """
    План опроса -> круг запросов (режим, номер). Вес w -- величина
    спрашивается раз в w кругов; круги склеиваются в цепочку длиной
    наименьшего общего кратного.
    """
    return [k for r in schedule_rounds(plan, supported, dids, mems)
            for k in r]


def schedule_rounds(plan, supported: set | None, dids: set | None = None,
                    mems: set | None = None) -> list:
    """То же, но по кругам: список кругов, в каждом -- запросы."""
    def ok(key):
        mode, ident = key
        if obd.describe(key) is None:
            return False
        if mode == 1:
            return supported is None or ident in supported
        if mode == 0x23:
            return mems is not None and ident in mems
        return dids is not None and ident in dids
    plan = [(k if ok(k) else obd.FALLBACK.get(k), w) for k, w in plan]
    plan = [(k, w) for k, w in plan if k is not None and ok(k)]
    if not plan:
        return []
    n = 1
    for _, w in plan:
        n = n * w // _gcd(n, w)
    n = min(n, 120)
    return [[p for p, w in plan if k % w == 0] for k in range(n)]


def _gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a


@dataclass
class EcuPoller:
    elm: object
    rec: Recorder
    clock: object
    plan: list = field(default_factory=lambda: list(obd.DEFAULT_PLAN))
    supported: set | None = None
    dids: set | None = None
    mems: set | None = None
    order: list = field(default_factory=list)
    pos: int = 0

    def __post_init__(self):
        self.rounds = schedule_rounds(self.plan, self.supported, self.dids,
                                      self.mems)
        self.order = [k for r in self.rounds for k in r]
        if not self.order:
            raise ValueError("блок не поддерживает ни одного нужного PID")
        self._rnd = random.Random(7)
        self._queue: list = []
        self._round = 0

    def step(self) -> None:
        # Порядок внутри круга каждый раз свой. Регулятор по узкому зонду
        # качает STFT с периодом около секунды, круг опроса -- тоже около
        # секунды, и при ровном порядке STFT читался бы раз за разом в одной
        # фазе, как стробоскопом: например, всегда около нуля. Тогда и
        # среднее врёт, и ровный ноль похож на разомкнутый контур.
        if not self._queue:
            r = list(self.rounds[self._round % len(self.rounds)])
            self._round += 1
            self._rnd.shuffle(r)
            self._queue = r
        key = self._queue.pop(0)
        self.pos += 1
        mode, ident = key
        p = obd.describe(key)
        t0 = self.clock()
        if mode == 1:
            body = self.elm.query(1, ident, p.nbytes)
        elif mode == 0x22:
            body = self.elm.query22(ident, p.nbytes)
        else:
            body = self.elm.query23(ident, p.nbytes)
        t1 = self.clock()
        if body is None:
            return
        t = (t0 + t1) / 2
        v = p.decode(bytes(body))
        if v is not None:
            self.rec.add(p.name, p.unit, t, v)
        for name, unit, dec in getattr(p, "extra", ()):
            v = dec(bytes(body))
            if v is not None:
                self.rec.add(name, unit, t, v)


@dataclass
class WbReader:
    t: object                   # Transport
    parser: object
    rec: Recorder
    clock: object
    count: int = 0
    bad: dict = field(default_factory=dict)    # состояние -> сколько
    status: str = ""
    last_t: float | None = None

    def step(self, timeout: float = 0.1) -> int:
        data = self.t.read(timeout)
        if not data:
            return 0
        now = self.clock()
        rd = self.parser.feed(data)
        if not rd:
            return 0
        # несколько показаний одним куском -- пришли с задержкой буфера;
        # раскладываем их равномерно от прошлого куска до этого
        start = self.last_t if self.last_t is not None else now
        span = max(0.0, now - start)
        n = len(rd)
        for i, r in enumerate(rd):
            t = now - span * (n - 1 - i) / n if n > 1 else now
            self.status = r.status
            if r.lam is not None:
                self.rec.add(LAMBDA_NAME, LAMBDA_UNIT, t, r.lam)
                self.count += 1
            else:
                self.bad[r.status] = self.bad.get(r.status, 0) + 1
        self.last_t = now
        return n


class LiveLogger:
    """
    Снятие лога. start()/stop() -- потоками, по настоящим часам;
    run_for() -- без потоков, по любым часам (проверки и имитатор).
    """

    def __init__(self, elm, wb_transport=None, wb_parser=None,
                 path: str = "", clock=None, plan=None,
                 supported: set | None = None, dids: set | None = None,
                 mems: set | None = None):
        self._clock = clock or time.monotonic
        self.t_start = self._clock()
        self.rec = Recorder(path)
        self.elm = elm
        if plan is None:
            plan = plan_for(dids or set(), mems, wb_transport is not None)
        self.ecu = EcuPoller(elm, self.rec, self.clock, list(plan),
                             supported, dids, mems)
        self.wb = (WbReader(wb_transport, wb_parser, self.rec, self.clock)
                   if wb_transport is not None else None)
        self._stop = threading.Event()
        self._threads: list = []
        self.error = ""
        self.wb_reopen = None       # как открыть порт ШДК заново
        self._fails = 0
        self.reconnects = 0

    def clock(self) -> float:
        return self._clock() - self.t_start

    # -- потоками -----------------------------------------------------------

    def start(self) -> None:
        self._stop.clear()
        self._threads = [threading.Thread(target=self._ecu_loop, daemon=True,
                                          name="elm")]
        if self.wb is not None:
            self._threads.append(threading.Thread(
                target=self._wb_loop, daemon=True, name="udk"))
        for th in self._threads:
            th.start()

    def _ecu_tick(self) -> None:
        """
        Один запрос к блоку. Если адаптер сбросился (LV RESET), зажигание
        выключали, шина или Bluetooth отвалились -- поднять заново и
        продолжать в тот же файл.
        """
        before = self.elm.stats.answered
        self.ecu.step()
        self._fails = 0 if self.elm.stats.answered > before else self._fails + 1
        # Блок молчит (NO DATA) -- адаптер на месте, ждём: зажигание могли
        # выключить на минуту. Молчит сам адаптер, без «>» -- пропал
        # Bluetooth или Wi-Fi, и ждать нечего: порт открывается заново.
        silent = self.elm.stats.silent >= 3
        if self.elm.stats.fatal or self._fails >= 30 or silent:
            self.error = ("связь с %s потеряна (%s), переподключаюсь" % (
                "адаптером" if silent else "блоком",
                self.elm.stats.fatal or self.elm.stats.last_error))
            self._fails = 0
            self.elm.stats.silent = 0
            try:
                self.elm.reinit()
                self.reconnects += 1
            except Exception as exc:                        # noqa: BLE001
                self.error = "переподключение не удалось: %s" % exc
                self.elm.stats.fatal = "переподключение не удалось"
                self._sleep(2.0)

    def _sleep(self, sec: float) -> None:
        if self._clock is time.monotonic:
            time.sleep(sec)
        elif hasattr(self._clock, "advance"):
            self._clock.advance(sec)

    def _ecu_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._ecu_tick()
            except Exception as exc:                        # noqa: BLE001
                self.error = "ELM: %s" % exc
                time.sleep(0.5)

    def _wb_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.wb.step(0.1)
            except Exception as exc:                        # noqa: BLE001
                # выдернули переходник, отвалился Bluetooth -- порт мёртв;
                # если умеем открыть заново -- открываем и пишем дальше
                self.error = "ШДК: %s" % exc
                time.sleep(1.0)
                if self.wb_reopen is not None:
                    try:
                        self.wb.t.close()
                    except Exception:                       # noqa: BLE001
                        pass
                    try:
                        self.wb.t = self.wb_reopen()
                        self.error = "ШДК: порт открыт заново"
                    except Exception as exc2:               # noqa: BLE001
                        self.error = "ШДК: порт не открывается: %s" % exc2
                        time.sleep(2.0)

    def stop(self) -> None:
        self._stop.set()
        for th in self._threads:
            th.join(timeout=3.0)
        self._threads = []
        self.rec.close()

    @property
    def running(self) -> bool:
        return any(th.is_alive() for th in self._threads)

    # -- без потоков ---------------------------------------------------------

    def run_for(self, seconds: float) -> None:
        end = self.clock() + seconds
        while self.clock() < end:
            self._ecu_tick()
            if self.wb is not None:
                self.wb.step(0.0)

    # -- что показать --------------------------------------------------------

    def rates(self, window: float = 5.0) -> dict:
        """Показаний в секунду по каналам за последние window секунд."""
        now = self.clock()
        out = {}
        with self.rec.lock:
            for name, ch in self.rec.channels.items():
                k = len(ch.t)
                i = k
                while i > 0 and ch.t[i - 1] >= now - window:
                    i -= 1
                span = min(window, now - ch.t[0]) if ch.t else 0
                out[name] = (k - i) / span if span > 0 else 0.0
        return out

    def latest(self) -> dict:
        with self.rec.lock:
            return dict(self.rec.latest)
