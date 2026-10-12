#!/usr/bin/env python3
"""
Машина-имитатор: ELM327 и УДК поверх модели мотора.

Логгер разговаривает с портами. Чтобы проверить его целиком -- от
текста ELM до строки в файле и поправки в прошивке -- без машины, здесь
есть оба собеседника:

* FakeElm -- отвечает как ELM327 v1.5 на K-линии: ATZ, ATE0, ATSP...,
  первый запрос со «SEARCHING...», PID режима 01 из модели мотора,
  «NO DATA» на неподдержанное, «?» на непонятное, задержка на запрос как
  у настоящей K-линии;
* FakeWideband -- шлёт пакеты Innovate ISP2 (или текст) 12 раз в секунду
  с лямбдой из той же модели, с задержкой датчика.

Часы бывают двух видов. В проверках -- VirtualClock: каждый запрос к ELM
двигает время на задержку K-линии, и пятиминутная поездка проезжается
за долю секунды. В демо-режиме окна -- настоящие часы и настоящее
ожидание, как в машине.
"""

from __future__ import annotations

import time

import obd
import udksim
import wideband
from elm import Transport

DEFAULT_SUPPORTED = {0x01, 0x03, 0x04, 0x05, 0x06, 0x07, 0x0C, 0x0D, 0x0E,
                     0x0F, 0x10, 0x11, 0x13, 0x14, 0x15, 0x1C, 0x20}


class VirtualClock:
    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class FakeCar:
    """Состояние мотора во времени: поездка из udksim, по кругу."""

    def __init__(self, fm, sensor_raw=None, err=udksim.err_lean_top,
                 drive=udksim.DRIVE, ltft=0.0, seed=1):
        self.truth = udksim.run(fm, sensor_raw or fm.hfm_raw, err=err,
                                drive=drive, seed=seed, ltft=ltft)
        self.dt = self.truth[1].t - self.truth[0].t
        self.period = len(self.truth) * self.dt

    def at(self, t: float):
        k = int((t % self.period) / self.dt)
        return self.truth[min(max(k, 0), len(self.truth) - 1)]

    def pid_value(self, pid: int, t: float) -> float:
        s = self.at(t)
        return {
            0x03: 2.0 if s.closed else 4.0,
            0x04: min(100.0, s.ml / max(s.rpm, 1) * 15),
            0x05: s.ect,
            0x06: s.stft,
            0x07: s.ltft,
            0x0C: s.rpm,
            0x0D: 60.0,
            0x0E: 15.0,
            0x0F: 25.0,
            0x10: s.ml / 3.6,
            0x11: s.tps,
            0x14: 0.45,
        }[pid]


class FakeElm(Transport):
    """ELM327 v1.5 на K-линии, ровно настолько, насколько нужно логгеру."""

    name = "имитатор ELM327"

    def __init__(self, car: FakeCar, clock, latency: float = 0.11,
                 realtime: bool = False, supported=None,
                 count_suffix: bool = True):
        self.car = car
        self.clock = clock
        self.latency = latency
        self.realtime = realtime
        self.supported = set(supported or DEFAULT_SUPPORTED)
        self.count_suffix = count_suffix
        self.inbuf = b""
        self.out = b""
        self.ready_at = 0.0
        self.echo = True
        self.spaces = True
        self.bus_up = False
        self.requests = 0

    def write(self, data: bytes) -> None:
        self.inbuf += data
        while b"\r" in self.inbuf:
            line, self.inbuf = self.inbuf.split(b"\r", 1)
            cmd = line.decode("ascii", "replace").strip().upper()
            reply, delay = self._answer(cmd)
            text = ((cmd + "\r") if self.echo else "") + reply + "\r\r>"
            self.out += text.encode("ascii")
            if self.realtime:
                self.ready_at = time.monotonic() + delay
            else:
                self.clock.advance(delay)

    def read(self, timeout: float) -> bytes:
        if self.realtime and self.out:
            wait = self.ready_at - time.monotonic()
            if wait > timeout:
                time.sleep(timeout)
                return b""
            if wait > 0:
                time.sleep(wait)
        elif not self.out and self.realtime:
            time.sleep(min(timeout, 0.01))
        data, self.out = self.out, b""
        return data

    def flush_input(self) -> None:
        pass

    def _hex(self, bs) -> str:
        sep = " " if self.spaces else ""
        return sep.join("%02X" % b for b in bs)

    def _answer(self, cmd: str) -> tuple[str, float]:
        c = cmd.replace(" ", "")
        if c.startswith("AT"):
            a = c[2:]
            if a in ("Z", "WS"):
                self.echo = True
                self.bus_up = False
                return "\r\rELM327 v1.5", 0.8
            if a.startswith("E"):
                self.echo = a == "E1"
            elif a.startswith("S") and a[1:] in ("0", "1"):
                self.spaces = a == "S1"
            elif a == "I":
                return "ELM327 v1.5", 0.01
            elif a == "DPN":
                return "A5", 0.01
            elif a == "RV":
                return "12.6V", 0.01
            return "OK", 0.01
        if not c:
            return "", 0.01
        if not all(ch in "0123456789ABCDEF" for ch in c):
            return "?", 0.01
        self.requests += 1
        prefix = ""
        delay = self.latency
        if not self.bus_up:
            prefix = "SEARCHING...\r"
            delay += 2.5
            self.bus_up = True
        if len(c) % 2 == 1:
            if not self.count_suffix:
                return "?", 0.01
            c = c[:-1]
        else:
            # без числа ответов ELM ждёт других блоков до таймаута
            delay += 0.05
        if len(c) != 4 or c[:2] != "01":
            return prefix + "NO DATA", delay
        pid = int(c[2:], 16)
        if pid == 0x00 or (pid in (0x20, 0x40) and pid in self.supported):
            body = obd.bitmap_for(self.supported, pid)
            return prefix + self._hex([0x41, pid] + list(body)), delay
        if pid not in self.supported or pid not in obd.PIDS:
            return prefix + "NO DATA", delay
        t = self.clock() if not self.realtime else time.monotonic()
        body = obd.encode(pid, self.car.pid_value(pid, t))
        return prefix + self._hex([0x41, pid] + list(body)), delay


class FakeWideband(Transport):
    """УДК: пакеты ISP2 или текстовые строки с лямбдой из модели."""

    name = "имитатор УДК"

    def __init__(self, car: FakeCar, clock, proto: str = "isp2",
                 lag: float = 0.3, rate: float = 12.2,
                 realtime: bool = False):
        self.car = car
        self.clock = clock
        self.proto = proto
        self.lag = lag
        self.period = 1.0 / rate
        self.realtime = realtime
        self.next_t = None
        self.warm_until = 2.0          # первые секунды датчик греется

    def _now(self) -> float:
        return time.monotonic() if self.realtime else self.clock()

    def _packet(self, t: float) -> bytes:
        s = self.car.at(t - self.lag)
        lam = min(s.lam, 1.6)
        if self.proto == "isp2":
            if t < self.warm_until:
                return wideband.isp2_packet(None, func=4)
            return wideband.isp2_packet(lam)
        if t < self.warm_until:
            return b""
        return ("%.2f\r\n" % (lam * 14.7)).encode("ascii")

    def read(self, timeout: float) -> bytes:
        now = self._now()
        if self.next_t is None:
            self.next_t = now
            if self.realtime:
                self.warm_until = now + 2.0
            else:
                self.warm_until = now + 2.0
        if self.realtime and self.next_t > now:
            time.sleep(min(timeout, self.next_t - now))
            now = self._now()
        out = b""
        while self.next_t <= now:
            out += self._packet(self.next_t)
            self.next_t += self.period
        return out
