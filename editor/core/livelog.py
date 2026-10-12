#!/usr/bin/env python3
"""
Логгер: блок через ELM и УДК через свой порт -- в один файл, на одних часах.

Зачем свой, а не Car Scanner. Car Scanner не видит УДК вовсе, и лог УДК
приходится снимать отдельно и потом сводить по времени. Здесь оба
источника пишутся одним процессом по одним часам, и сводить нечего:
остаётся только задержка самого датчика, она задаётся при расчёте.

Как устроено:

* поток ELM -- спрашивает PID по плану: обороты, расход и дроссель каждый
  круг, STFT через круг, LTFT и ОЖ редко. По K-линии на всё про всё около
  десяти запросов в секунду, и делить их поровну с температурой ОЖ --
  расточительство;
* поток УДК -- читает порт, разбирает протокол, пишет годную лямбду;
* запись -- сразу в файл, построчно, в том же формате, что у Car Scanner
  ("SECONDS";"PID";"VALUE";"UNITS"). Поэтому лог читается тем же
  разбором, падение программы не теряет снятое, а метка времени у
  каждого показания своя -- без выдуманного выравнивания.

Метка показания ELM -- середина между запросом и ответом: блок отдаёт
значение где-то в этом промежутке, середина ошибается меньше всего.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field

import logio
import obd

LAMBDA_NAME = "УДК лямбда"
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


def schedule(plan, supported: set | None) -> list[int]:
    """
    План опроса -> круг запросов. Вес w -- PID спрашивается раз в w кругов;
    круги склеиваются в одну цепочку длиной наименьшего общего кратного.
    """
    plan = [(p, w) for p, w in plan
            if p in obd.PIDS and (supported is None or p in supported)]
    if not plan:
        return []
    n = 1
    for _, w in plan:
        n = n * w // _gcd(n, w)
    n = min(n, 120)
    out = []
    for k in range(n):
        out.extend(p for p, w in plan if k % w == 0)
    return out


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
    order: list = field(default_factory=list)
    pos: int = 0

    def __post_init__(self):
        self.order = schedule(self.plan, self.supported)
        if not self.order:
            raise ValueError("блок не поддерживает ни одного нужного PID")

    def step(self) -> None:
        pid = self.order[self.pos % len(self.order)]
        self.pos += 1
        p = obd.PIDS[pid]
        t0 = self.clock()
        body = self.elm.query(1, pid, p.nbytes)
        t1 = self.clock()
        if body is None:
            return
        v = p.decode(bytes(body))
        if v is not None:
            self.rec.add(p.name, p.unit, (t0 + t1) / 2, v)


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
                 supported: set | None = None):
        self._clock = clock or time.monotonic
        self.t_start = self._clock()
        self.rec = Recorder(path)
        self.elm = elm
        self.ecu = EcuPoller(elm, self.rec, self.clock,
                             list(plan or obd.DEFAULT_PLAN), supported)
        self.wb = (WbReader(wb_transport, wb_parser, self.rec, self.clock)
                   if wb_transport is not None else None)
        self._stop = threading.Event()
        self._threads: list = []
        self.error = ""

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

    def _ecu_loop(self) -> None:
        fails = 0
        while not self._stop.is_set():
            try:
                before = self.elm.stats.answered
                self.ecu.step()
                fails = 0 if self.elm.stats.answered > before else fails + 1
                if fails >= 30:
                    self.error = ("блок не отвечает 30 запросов подряд: %s"
                                  % self.elm.stats.last_error)
                    time.sleep(0.5)
            except Exception as exc:                        # noqa: BLE001
                self.error = "ELM: %s" % exc
                time.sleep(0.5)

    def _wb_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.wb.step(0.1)
            except Exception as exc:                        # noqa: BLE001
                self.error = "УДК: %s" % exc
                time.sleep(0.5)

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
            self.ecu.step()
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
