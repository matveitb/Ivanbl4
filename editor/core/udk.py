#!/usr/bin/env python3
"""
Смесь по УДК: лог езды -> поправки в прошивку.

Что считается
-------------
Блок не измеряет топливо, он его вычисляет:

    топливо = воздух_по_ДМРВ / (14.7 * лямбда_заданная) * коррекции

Широкополосный датчик (УДК) показывает, что получилось на самом деле.
Отсюда одна величина на каждый момент лога -- ОШИБКА ВОЗДУХА:

    e = лямбда_УДК * (1 + STFT) * (1 + LTFT) / лямбда_заданная

e = 1.10 значит, что воздуха в цилиндре на 10 % больше, чем насчитал
блок, и топлива ему не хватает на те же 10 %. Коррекции STFT и LTFT в
формуле потому, что на частичных блок сам доливает по узкому зонду, и УДК
там показывает единицу при любой ошибке ДМРВ -- ошибка видна только в
коррекциях.

Чем править (выбирается)
------------------------
* ДМРВ, `HFM_LIN` -- тарировка: расход по напряжению. Поправка e
  раскладывается по расходу, и таблица умножается на неё. Чинит причину
  (неверно измеренный воздух) сразу во всех режимах, и нагрузка для УОЗ
  становится честной. Это классическая «калибровка ДМРВ по УДК».
* Полная нагрузка, `KLLAMFA_0/_1` -- цель по лямбде при газе в пол, по
  оборотам. Новая цель такая, чтобы УДК показал желаемую лямбду:

      цель_новая = лямбда_желаемая * цель_была / лямбда_УДК_без_STFT

  Чинит только полный газ, ничего больше не трогает. Быстро и
  безопасно, но ошибку ДМРВ не убирает, а прячет.

Заданная лямбда -- из прошивки, по коду
---------------------------------------
По OBD блок её не отдаёт (обработчик режима 01 на 0x864xxx не читает ни
0x9818, ни 0x8A16), поэтому она восстанавливается из калибровки, ровно
как её считает блок (docs/32, docs/33, декомпиляция 0x855B5E):

* полная нагрузка -- дроссель выше `WDKVLN`(обороты). Байт дросселя тот
  же, что ELM отдаёт в PID 0x11 (0x89F3, обработчик 0x864C18), поэтому
  сравнение с порогом точное;
* на полной нагрузке запрос `KLLAMFA`(обороты); задержки нет --
  0x1B894 равен нулю во всех прошивках;
* запрос сводится МИНИМУМОМ с лямбдой защиты компонентов 0x98A2, а она
  равна 1 - (1 - KFLBTS) * разрешение и потому не выше 1.000. Значит
  KLLAMFA больше единицы не действует: у v2M и MPower 1.023 по факту
  дают 1.000, то есть обогащения на полном газе нет вовсе;
* насколько разрешена защита компонентов, зависит от времени и модели
  температуры -- из лога не видно. Поэтому есть ключ: считать её
  выключенной (по умолчанию) или включённой полностью.

Расход в логе -- это `ml` 0xF868, уже умноженный на поправку пульсаций
и `KFKHFM`: обработчик PID 0x10 на 0x864BEC делит 0xF868 на 9 и множит
на 25, то есть переводит 0.1 кг/ч в 0.01 г/с. Ключ поправки ДМРВ из-за
этого смещён на те же несколько процентов, что и пульсационная карта;
на значение поправки это не влияет, а форма кривой гладкая.
"""

from __future__ import annotations

import bisect
import math
import os
import re
import sys
from collections import OrderedDict
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logio                                                # noqa: E402
import mapaccess as M                                       # noqa: E402

STOICH = 14.7
KGH_PER_GS = 3.6

# -- роли каналов -----------------------------------------------------------

# (роль, подпись, обязательна)
ROLES = [
    ("rpm", "Обороты", True),
    ("maf", "Расход воздуха (ДМРВ)", True),
    ("tps", "Дроссель", True),
    ("lam", "УДК: лямбда или AFR", True),
    ("stft", "Быстрая коррекция (STFT)", False),
    ("ltft", "Адаптация (LTFT)", False),
    ("ect", "Температура ОЖ", False),
]
ROLE_TITLES = {r: t for r, t, _ in ROLES}

_PAT = {
    "rpm": (r"rpm|об/мин|оборот|engine speed|nmot|частота вращ", r"vehicle|скорост|км/ч|km/h"),
    "maf": (r"\bmaf\b|mass air|air ?flow|расход возд|массов\w* расход|дмрв|\bml\b",
            r"volt|напряж|\(v\)|, ?в$|voltage|correct|коррек"),
    "tps": (r"throttle|дросс|\btps\b|\bдз\b",
            r"relative|относит|command|задан|педал|pedal|accel|\bb\)|\bb$|volt|напряж"),
    "lam": (r"lambda|лямбд|λ|\bудк\b|\bшдк\b|\bafr\b|wideband|\blc-?[12]\b|wbo2|air.?fuel|соста\w+ смеси",
            r"command|задан|equivalence|o2 sensor|кислород|volt|напряж|target|цел"),
    "stft": (r"short.?term|\bstft\b|кратко|быстр\w* корр", r"bank ?2|банк ?2"),
    "ltft": (r"long.?term|\bltft\b|долго|адаптац", r"bank ?2|банк ?2"),
    "ect": (r"coolant|охлажд|\bож\b|\bтож\b|engine temp|температура двиг|\bect\b",
            r"\bvolt"),
}


def detect_roles(ecu: logio.LogFile, wb: logio.LogFile | None) -> dict:
    """
    Какой канал за что отвечает -- по имени и единицам.

    Возвращает роль -> (источник, имя канала) или None. УДК ищется
    сначала в отдельном логе: если человек его дал, лямбда там.
    """
    out = {r: None for r, _, _ in ROLES}
    sources = [("ecu", ecu)] + ([("wb", wb)] if wb is not None else [])
    for role, (pat, bad) in _PAT.items():
        rx, rbad = re.compile(pat, re.I), re.compile(bad, re.I)
        order = list(reversed(sources)) if role == "lam" else sources
        cands = []
        for src, log in order:
            for name, ch in log.channels.items():
                lab = "%s %s" % (name, ch.unit)
                if rx.search(lab) and not rbad.search(lab):
                    cands.append((src, name))
            if cands:
                break
        if role == "lam" and len(cands) > 1:
            # из «лямбды» и «AFR» одного логгера берём лямбду
            lam_first = [c for c in cands
                         if re.search(r"lambda|лямбд|λ", c[1], re.I)]
            cands = lam_first or cands
        if cands:
            out[role] = cands[0]
    return out


def lam_scale(ch: logio.Channel, kind: str = "auto",
              stoich: float = STOICH) -> tuple[float, str]:
    """Множитель к лямбде и что это было: λ или AFR."""
    if kind == "lambda":
        return 1.0, "λ"
    if kind == "afr":
        return 1.0 / stoich, "AFR"
    lab = ("%s %s" % (ch.name, ch.unit)).lower()
    vals = sorted(ch.v)
    med = vals[len(vals) // 2] if vals else 1.0
    if med > 4.0 or ("afr" in lab and not re.search(r"lambda|лямбд|λ", lab)):
        return 1.0 / stoich, "AFR"
    return 1.0, "λ"


def maf_scale(ch: logio.Channel, unit: str = "auto") -> tuple[float, str]:
    """Множитель расхода к кг/ч."""
    if unit == "kg/h":
        return 1.0, "кг/ч"
    if unit == "g/s":
        return KGH_PER_GS, "г/с"
    lab = ("%s %s" % (ch.name, ch.unit)).lower()
    if re.search(r"кг/ч|kg/h|kg/hr", lab):
        return 1.0, "кг/ч"
    if re.search(r"g/s|г/с|гр/с|g/sec", lab):
        return KGH_PER_GS, "г/с"
    # 1.6 на полном газе -- около 100 г/с, на ХХ -- около 10 кг/ч
    return (1.0, "кг/ч") if (ch.v and max(ch.v) > 150) else (KGH_PER_GS, "г/с")


# -- модель блока -----------------------------------------------------------

def interp1(xs, ys, x: float) -> float:
    """Линейно с зажимом по краям -- так интерполирует блок."""
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    i = bisect.bisect_right(xs, x)
    a, b = xs[i - 1], xs[i]
    k = (x - a) / (b - a) if b > a else 0.0
    return ys[i - 1] + (ys[i] - ys[i - 1]) * k


def hat(xs, x: float) -> list[tuple[int, float]]:
    """Веса узлов для точки x: те же, с какими блок интерполирует."""
    if x <= xs[0]:
        return [(0, 1.0)]
    if x >= xs[-1]:
        return [(len(xs) - 1, 1.0)]
    i = bisect.bisect_right(xs, x)
    a, b = xs[i - 1], xs[i]
    k = (x - a) / (b - a) if b > a else 0.0
    return [(i - 1, 1.0 - k), (i, k)]


@dataclass
class FuelModel:
    """Всё, что нужно из прошивки, чтобы знать, какую лямбду хотел блок."""
    kllamfa_x: list
    kllamfa: list            # [вариант] -> значения
    wdkvln_x: list
    wdkvln: list             # [вариант] -> % дросселя
    kflbts_rpm: list
    kflbts_load: list
    kflbts: list             # строки -- нагрузка, столбцы -- обороты
    hfm_raw: list            # 512 слов
    hfm_factor: float
    hfm_ofs: float           # кг/ч
    kumsrl: float            # (кг/ч)/(1/мин)/%
    variant: int = 0
    kllamfa_factor: float = 0.0078125
    _flows: list = None      # расход по точкам тарировки, считается раз

    NAMES = ("KLLAMFA_0", "KLLAMFA_1", "WDKVLN_0", "WDKVLN_1", "KFLBTS",
             "HFM_LIN", "HFM_LIN_OFS", "KUMSRL")

    @classmethod
    def from_project(cls, p, variant: int = 0) -> "FuelModel":
        miss = [n for n in cls.NAMES if n not in p.layouts]
        if miss:
            raise KeyError("в описании нет карт: " + ", ".join(miss))
        buf = p.buf

        def curve(name):
            L = p.layout(name)
            xs, _ = M.axes(buf, L)
            return list(xs[:L.nx]), M.read_phys(buf, L)[0]

        kx, k0 = curve("KLLAMFA_0")
        _, k1 = curve("KLLAMFA_1")
        wx, w0 = curve("WDKVLN_0")
        _, w1 = curve("WDKVLN_1")
        L = p.layout("KFLBTS")
        bx, by = M.axes(buf, L)
        hl = p.layout("HFM_LIN")
        return cls(kllamfa_x=kx, kllamfa=[k0, k1], wdkvln_x=wx,
                   kllamfa_factor=p.layout("KLLAMFA_0").factor,
                   wdkvln=[w0, w1], kflbts_rpm=list(bx[:L.nx]),
                   kflbts_load=list(by[:L.ny]),
                   kflbts=M.read_phys(buf, L),
                   hfm_raw=M.read_raw(buf, hl)[0], hfm_factor=hl.factor,
                   hfm_ofs=M.read_phys(buf, p.layout("HFM_LIN_OFS"))[0][0],
                   kumsrl=M.read_phys(buf, p.layout("KUMSRL"))[0][0],
                   variant=variant)

    # -- режим и цель -------------------------------------------------------

    def wdkvln_at(self, rpm: float) -> float:
        return interp1(self.wdkvln_x, self.wdkvln[self.variant], rpm)

    def full_load(self, rpm: float, tps: float) -> bool:
        return tps > self.wdkvln_at(rpm)

    def kllamfa_at(self, rpm: float) -> float:
        return interp1(self.kllamfa_x, self.kllamfa[self.variant], rpm)

    def kflbts_at(self, rpm: float, rl: float) -> float:
        row = [interp1(self.kflbts_rpm, r, rpm) for r in self.kflbts]
        return interp1(self.kflbts_load, row, rl)

    def lam_cmd(self, rpm: float, tps: float, rl: float,
                bts: bool = False) -> tuple[float, bool]:
        """Заданная лямбда и признак полной нагрузки."""
        fl = self.full_load(rpm, tps)
        lam = min(self.kllamfa_at(rpm), 1.0) if fl else 1.0
        if bts:
            lam = min(lam, self.kflbts_at(rpm, rl))
        return lam, fl

    def rl(self, rpm: float, ml_kgh: float) -> float:
        """Наполнение, % -- по той же формуле, что у блока (docs/36)."""
        return ml_kgh / (rpm * self.kumsrl) if rpm > 0 and self.kumsrl else 0.0

    # -- тарировка ДМРВ ----------------------------------------------------

    def hfm_flow(self, i: int, raw=None) -> float:
        raw = self.hfm_raw if raw is None else raw
        return raw[i] * self.hfm_factor - self.hfm_ofs

    def hfm_flows(self) -> list:
        if self._flows is None or len(self._flows) != len(self.hfm_raw):
            self._flows = [self.hfm_flow(i) for i in range(len(self.hfm_raw))]
        return self._flows

    def hfm_index(self, q: float) -> float | None:
        """Дробный номер точки таблицы, где расход равен q, кг/ч."""
        n = len(self.hfm_raw)
        flows = self.hfm_flows()
        if q <= flows[0] or q <= 0:
            return None
        if q >= flows[-1]:
            return float(n - 1)
        i = bisect.bisect_left(flows, q)
        a, b = flows[i - 1], flows[i]
        return (i - 1) + ((q - a) / (b - a) if b > a else 0.0)

    @staticmethod
    def hfm_volts(i: float) -> float:
        """Номер точки -> напряжение: индекс = АЦП/2, АЦП 10 бит на 5 В."""
        return i * 2 * 5.0 / 1023


# -- настройки и результат ----------------------------------------------------

@dataclass
class Settings:
    target: str = "kllamfa"     # "kllamfa" | "hfm"
    lam_want: float = 0.88      # желаемая лямбда на полном газе
    lag: float = 0.3            # задержка УДК относительно ЭБУ, с
    offset: float = 0.0         # время ЭБУ = время УДК + offset (отдельный лог)
    dt: float = 0.1             # шаг общей шкалы времени, с
    max_gap: float = 2.0        # дальше этого между показаниями -- нет данных
    ect_min: float = 70.0       # холоднее -- прогревное обогащение
    rpm_min: float = 600.0
    tps_min: float = 1.5        # ниже и выше cut_rpm -- накат, сброс топлива
    cut_rpm: float = 1300.0
    lam_lo: float = 0.60
    lam_hi: float = 1.60
    tps_rate: float = 40.0      # %/с: быстрее -- рывок дросселя
    settle: float = 1.0         # с после рывка не верим
    min_time: float = 0.5       # с данных на узел
    max_rich: float = 25.0      # % обогащения за один проход
    max_lean: float = 10.0      # % обеднения за один проход
    fill_empty: bool = True     # продлить на узлы без данных
    bts: bool = False           # защита компонентов работала полностью
    lam_kind: str = "auto"      # "auto" | "lambda" | "afr"
    maf_unit: str = "auto"      # "auto" | "g/s" | "kg/h"
    stoich: float = STOICH
    variant: int = 0
    hfm_step: int = 16          # точек тарировки на узел поправки


@dataclass
class Sample:
    t: float
    rpm: float
    maf: float          # кг/ч
    tps: float
    lam: float
    stft: float | None
    ltft: float | None
    fl: bool
    lcmd: float
    rl: float
    e: float            # ошибка воздуха


@dataclass
class Node:
    x: float            # обороты или номер точки тарировки
    label: str
    time: float = 0.0   # с данных (взвешенных)
    lam: float = 0.0    # средняя лямбда УДК
    lcmd: float = 0.0   # средняя заданная
    err: float = 0.0    # средняя ошибка воздуха e
    want: float = 0.0   # что требуют данные (до пределов)
    old: float = 1.0
    new: float = 1.0
    status: str = ""

    @property
    def change(self) -> float:
        return (self.new / self.old - 1.0) * 100 if self.old else 0.0


@dataclass
class Band:
    lo: float
    hi: float
    time: float = 0.0
    lam: float = 0.0
    lmin: float = 9.9
    lmax: float = 0.0
    lcmd: float = 0.0
    err: float = 0.0


@dataclass
class Result:
    settings: Settings
    target: str
    nodes: list = field(default_factory=list)
    samples: list = field(default_factory=list)
    used: list = field(default_factory=list)     # что пошло в поправку
    rejected: OrderedDict = field(default_factory=OrderedDict)  # причина -> с
    total: float = 0.0          # секунд общей шкалы
    bands: list = field(default_factory=list)    # полная нагрузка по оборотам
    warnings: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    hfm_new: list = field(default_factory=list)  # новая тарировка, сырые
    kllamfa_new: list = field(default_factory=list)

    @property
    def accepted(self) -> float:
        return len(self.samples) * self.settings.dt

    @property
    def changed(self) -> bool:
        return any(abs(n.new - n.old) > 1e-9 for n in self.nodes)


# -- сведение логов ------------------------------------------------------------

def _channel(ecu, wb, ref):
    if not ref:
        return None
    src, name = ref
    log = ecu if src == "ecu" else wb
    return log.channels.get(name) if log is not None else None


def _transients(tps: logio.Channel, st: Settings) -> list[tuple[float, float]]:
    """Отрезки времени, когда дроссель дёргали: туда не смотрим."""
    out = []
    t, v = tps.t, tps.v
    for i in range(1, len(t)):
        span = t[i] - t[i - 1]
        if span <= 0 or span > 2 * st.max_gap:
            continue
        if abs(v[i] - v[i - 1]) / span > st.tps_rate:
            out.append((t[i - 1], t[i] + st.settle))
    out.sort()
    merged = []
    for a, b in out:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def _inside(spans, starts, t: float) -> bool:
    i = bisect.bisect_right(starts, t) - 1
    return i >= 0 and spans[i][0] <= t <= spans[i][1]


def _reject(res: Result, why: str, dt: float) -> None:
    res.rejected[why] = res.rejected.get(why, 0.0) + dt


REASONS = ("нет показаний", "холодный мотор", "обороты ниже порога",
           "дроссель закрыт, сброс топлива", "УДК вне диапазона",
           "рывок дросселя", "нет расхода")


def merge(fm: FuelModel, ecu: logio.LogFile, wb: logio.LogFile | None,
          roles: dict, st: Settings, res: Result) -> list[Sample]:
    """Свести каналы на общую шкалу, отбраковать, посчитать e."""
    ch = {r: _channel(ecu, wb, roles.get(r)) for r, _, _ in ROLES}
    need = [ROLE_TITLES[r] for r, _, must in ROLES if must and ch[r] is None]
    if need:
        raise ValueError("в логе не найдено: " + ", ".join(need))

    lam_k, lam_kind = lam_scale(ch["lam"], st.lam_kind, st.stoich)
    maf_k, maf_unit = maf_scale(ch["maf"], st.maf_unit)
    res.notes.append("УДК: %s «%s»%s" % (
        lam_kind, ch["lam"].name,
        (", делю на %.1f" % st.stoich) if lam_kind == "AFR" else ""))
    res.notes.append("расход: «%s» в %s" % (ch["maf"].name, maf_unit))

    # лямбда: из своего лога на часы ЭБУ, и раньше на время задержки --
    # УДК видит газ, который сгорел lag секунд назад
    shift = -st.lag
    if roles["lam"][0] == "wb":
        shift += st.offset
    lam_ch = logio.shifted(ch["lam"], shift)

    for r in ("stft", "ltft", "ect"):
        if ch[r] is None:
            res.warnings.append("%s в логе нет" % ROLE_TITLES[r])

    spans = _transients(ch["tps"], st)
    starts = [a for a, _ in spans]

    t0 = max(ch["rpm"].t[0], ch["maf"].t[0], ch["tps"].t[0], lam_ch.t[0])
    t1 = min(ch["rpm"].t[-1], ch["maf"].t[-1], ch["tps"].t[-1], lam_ch.t[-1])
    if t1 <= t0:
        raise ValueError("лог ЭБУ и лог УДК не пересекаются по времени -- "
                         "проверьте сдвиг")
    for why in REASONS:
        res.rejected[why] = 0.0

    out = []
    n = int((t1 - t0) / st.dt) + 1
    res.total = n * st.dt
    g = st.max_gap
    for k in range(n):
        t = t0 + k * st.dt
        rpm = logio.sample(ch["rpm"], t, g)
        maf = logio.sample(ch["maf"], t, g)
        tps = logio.sample(ch["tps"], t, g)
        lam = logio.sample(lam_ch, t, g)
        if None in (rpm, maf, tps, lam):
            _reject(res, "нет показаний", st.dt)
            continue
        maf *= maf_k
        lam *= lam_k
        if ch["ect"] is not None:
            ect = logio.sample(ch["ect"], t, 30.0)
            if ect is not None and ect < st.ect_min:
                _reject(res, "холодный мотор", st.dt)
                continue
        if rpm < st.rpm_min:
            _reject(res, "обороты ниже порога", st.dt)
            continue
        if tps < st.tps_min and rpm > st.cut_rpm:
            # закрытый дроссель выше ХХ -- накат, топливо перекрыто или
            # вот-вот вернётся; на самом ХХ закрытый дроссель -- норма
            _reject(res, "дроссель закрыт, сброс топлива", st.dt)
            continue
        if not (st.lam_lo <= lam <= st.lam_hi):
            _reject(res, "УДК вне диапазона", st.dt)
            continue
        if spans and _inside(spans, starts, t):
            _reject(res, "рывок дросселя", st.dt)
            continue
        if maf <= 0.5:
            _reject(res, "нет расхода", st.dt)
            continue
        stft = (logio.sample(ch["stft"], t, g)
                if ch["stft"] is not None else None)
        ltft = (logio.sample(ch["ltft"], t, g)
                if ch["ltft"] is not None else None)
        rl = fm.rl(rpm, maf)
        lcmd, fl = fm.lam_cmd(rpm, tps, rl, st.bts)
        trims = (1 + (stft or 0.0) / 100) * (1 + (ltft or 0.0) / 100)
        out.append(Sample(t, rpm, maf, tps, lam, stft, ltft, fl, lcmd, rl,
                          lam * trims / lcmd))
    return out


# -- сведение двух логов по событиям --------------------------------------------

def _events(t0, t1, step, on, off, getv):
    """Моменты входа в состояние: on(x) стало истинно после off(x)."""
    out, armed = [], False
    t = t0
    while t <= t1:
        x = getv(t)
        if x is not None:
            if armed and on(x):
                out.append(t)
                armed = False
            elif off(x):
                armed = True
        t += step
    return out


def _match(ev_a, ev_b, cand, tol):
    """Пары событий при сдвиге cand: (a, b) с |a - (b + cand)| <= tol."""
    pairs = []
    for x in ev_a:
        y = x - cand
        j = bisect.bisect_left(ev_b, y)
        near = [ev_b[q] for q in (j - 1, j) if 0 <= q < len(ev_b)]
        if near:
            z = min(near, key=lambda v: abs(v - y))
            if abs(z - y) <= tol:
                pairs.append((x, z))
    return pairs


def find_offset(ecu: logio.LogFile, wb: logio.LogFile, roles: dict,
                st: Settings) -> tuple[float | None, str]:
    """
    Сдвиг отдельного лога УДК: время ЭБУ = время УДК + сдвиг.

    Опора -- КОНЕЦ сброса топлива. Катились накатом с закрытым дросселем
    -- форсунки перекрыты, УДК лежит в бедном упоре. Нажали газ -- топливо
    вернулось сразу, и УДК падает к единице. В логе ЭБУ это виден подъём
    дросселя после закрытого на оборотах, в логе УДК -- падение лямбды из
    упора. Таких событий за поездку десятки, и совпасть все разом при
    неверном сдвиге не могут.

    Начало сброса для этого не годится: Bosch перекрывает топливо не
    сразу, а с выдержкой, и она сместила бы сдвиг на неё. Возврат топлива
    по газу выдержки не имеет, между событиями только задержка самого УДК.

    Если событий мало, а у обоих логов есть часы, берём разницу часов.
    """
    rpm = _channel(ecu, None, roles.get("rpm"))
    tps = _channel(ecu, None, roles.get("tps"))
    lam = _channel(ecu, wb, roles.get("lam"))
    if rpm is None or tps is None or lam is None or roles["lam"][0] != "wb":
        return None, "нужен лог ЭБУ с оборотами и дросселем и отдельный лог УДК"
    k, _ = lam_scale(lam, st.lam_kind, st.stoich)
    g = st.max_gap

    # Событие ЭБУ берётся по СЫРЫМ показаниям дросселя, серединой между
    # последним «закрыт» и первым «открыт». Интерполяция тут врёт: ELM
    # опрашивает дроссель раз в секунду, газ открывают рывком, и линейный
    # подъём между опросами пересекает 3 % в самом начале отрезка -- на
    # четверть секунды раньше, чем в среднем было на деле.
    ev_ecu = []
    closed_at = None
    for t, v in zip(tps.t, tps.v):
        r = logio.sample(rpm, t, g)
        if v < st.tps_min and r is not None and r > 1400:
            closed_at = t
        elif v > 3.0 and closed_at is not None:
            if t - closed_at <= 2 * g:
                ev_ecu.append((closed_at + t) / 2)
            closed_at = None
    ev_wb = _events(lam.t[0], lam.t[-1], 0.02,
                    lambda x: x * k < 1.15, lambda x: x * k > 1.45,
                    lambda t: logio.sample(lam, t, g))
    # события лога ЭБУ на шкале УДК сдвинуты ещё и на задержку датчика
    ev_ecu = [x + st.lag for x in ev_ecu]

    clock = None
    if (ecu.t0 is not None and wb.t0 is not None
            and ecu.t0_kind == wb.t0_kind):
        clock = wb.t0 - ecu.t0

    if len(ev_ecu) >= 2 and len(ev_wb) >= 2:
        # допуск -- не меньше шага, с которым ELM опрашивает дроссель
        gaps = sorted(b - a for a, b in zip(tps.t, tps.t[1:]) if b > a)
        tol = max(0.6, 1.2 * gaps[len(gaps) // 2]) if gaps else 0.6
        best = None
        for a in ev_ecu:
            for b in ev_wb:
                cand = a - b
                pairs = _match(ev_ecu, ev_wb, cand, tol)
                score = (len(pairs),
                         -sum(abs(x - z - cand) for x, z in pairs))
                if best is None or score > best[0]:
                    best = (score, cand)
        (cnt, _), cand = best
        if cnt >= 2 and cnt >= 0.5 * min(len(ev_ecu), len(ev_wb)):
            # уточнить средним по совпавшим парам, дважды: после первого
            # уточнения могут добрать пары, бывшие на краю допуска
            for _ in range(2):
                pairs = _match(ev_ecu, ev_wb, cand, tol)
                cand = sum(x - z for x, z in pairs) / len(pairs)
            return cand, ("по возвратам топлива после наката: совпало %d "
                          "из %d" % (len(pairs), len(ev_ecu)))
    if clock is not None:
        return clock, "по часам логов"
    return None, ("опорных событий мало (ЭБУ %d, УДК %d), часов нет -- "
                  "сдвиг задайте руками" % (len(ev_ecu), len(ev_wb)))


# -- поправки -------------------------------------------------------------------

def _accumulate(xs, samples, key, value):
    """Взвешенные суммы по узлам: время, лямбда, цель, ошибка, требование."""
    acc = [[0.0] * 5 for _ in xs]
    for s in samples:
        x = key(s)
        if x is None:
            continue
        v = value(s)
        for i, w in hat(xs, x):
            a = acc[i]
            a[0] += w
            a[1] += w * s.lam
            a[2] += w * s.lcmd
            a[3] += w * s.e
            a[4] += w * v
    return acc


def _fill(nodes: list[Node], interior: bool, above: bool, below: str) -> None:
    """
    Узлы без данных. Между узлами с данными -- линейно, выше последнего --
    как у последнего (если разрешено), ниже первого -- `below`:
    "hold" -- как у первого, "one" -- без изменения.
    """
    have = [i for i, n in enumerate(nodes) if n.status == "по данным"
            or n.status.startswith("упёрлось")]
    if not have:
        return
    for i, n in enumerate(nodes):
        if i in have:
            continue
        lo = max((j for j in have if j < i), default=None)
        hi = min((j for j in have if j > i), default=None)
        ratio = None
        if lo is not None and hi is not None and interior:
            a, b = nodes[lo], nodes[hi]
            k = (n.x - a.x) / (b.x - a.x) if b.x != a.x else 0.0
            ra, rb = a.new / a.old, b.new / b.old
            ratio, n.status = ra + (rb - ra) * k, "между соседями"
        elif hi is None and lo is not None and above:
            ratio, n.status = nodes[lo].new / nodes[lo].old, "как у соседа"
        elif lo is None and hi is not None and below == "hold":
            ratio, n.status = nodes[hi].new / nodes[hi].old, "как у соседа"
        if ratio is not None:
            n.new = n.old * ratio


def _limit(want: float, old: float, st: Settings) -> tuple[float, bool]:
    """
    Не больше max_rich % в богатую и max_lean % в бедную за проход.

    Несимметрично намеренно: бедная смесь на полном газе прожигает
    поршни, богатая -- только тратит бензин. Обогащать смело, обеднять
    по шагу.
    """
    lo = old * (1 - st.max_rich / 100)
    hi = old * (1 + st.max_lean / 100)
    v = min(max(want, lo), hi)
    return v, abs(v - want) > 1e-9


def correct_kllamfa(fm: FuelModel, samples: list[Sample], st: Settings,
                    res: Result) -> None:
    xs = fm.kllamfa_x
    used = [s for s in samples if s.fl]
    res.used = used
    if not used:
        res.warnings.append(
            "в логе нет ни одной секунды полной нагрузки: блок признаёт её, "
            "когда дроссель выше WDKVLN (%s %%) -- поправлять KLLAMFA "
            "не по чему" % "/".join("%.0f" % v for v in fm.wdkvln[fm.variant]))

    def required(s: Sample) -> float:
        # цель, при которой УДК покажет желаемое; STFT на полном газе
        # выключается вместе с переходом на богатую цель, его вклад убираем
        return st.lam_want * s.lcmd / (s.lam * (1 + (s.stft or 0.0) / 100))

    acc = _accumulate(xs, used, lambda s: s.rpm, required)
    cur = fm.kllamfa[fm.variant]
    for i, x in enumerate(xs):
        w = acc[i][0]
        old = min(cur[i], 1.0)     # выше единицы не действует
        n = Node(x=x, label="%.0f" % x, old=old, new=old,
                 time=w * st.dt)
        if w > 0:
            n.lam, n.lcmd, n.err, n.want = (acc[i][1] / w, acc[i][2] / w,
                                            acc[i][3] / w, acc[i][4] / w)
        if n.time >= st.min_time:
            n.new, hit = _limit(n.want, old, st)
            n.new = min(max(n.new, 0.70), 1.10)
            n.status = "упёрлось в предел шага" if hit else "по данным"
        else:
            n.status = "мало данных" if w > 0 else "нет данных"
        res.nodes.append(n)
    _fill(res.nodes, True, st.fill_empty, "hold" if st.fill_empty else "one")
    # показываем то, что ляжет в файл, а не то, что насчитали: шаг 1/128
    q = fm.kllamfa_factor
    for n in res.nodes:
        if abs(n.new - n.old) > 1e-9:
            n.new = round(n.new / q) * q
    res.kllamfa_new = [n.new for n in res.nodes]


def correct_hfm(fm: FuelModel, samples: list[Sample], st: Settings,
                res: Result) -> None:
    have_trims = any(s.stft is not None for s in samples)
    if have_trims:
        used = list(samples)
    else:
        used = [s for s in samples if s.fl]
        res.warnings.append(
            "STFT в логе нет: на частичных блок доливает по узкому зонду, и "
            "без коррекций ошибка ДМРВ там не видна. Для ДМРВ взяты только "
            "точки полной нагрузки")
    res.used = used
    n = len(fm.hfm_raw)
    step = max(2, int(st.hfm_step))
    xs = list(range(0, n, step))
    if xs[-1] != n - 1:
        xs.append(n - 1)
    acc = _accumulate(xs, used, lambda s: fm.hfm_index(s.maf),
                      lambda s: s.e)
    for i, x in enumerate(xs):
        w = acc[i][0]
        q = fm.hfm_flow(x)
        nd = Node(x=x, label="%.2f В, %.0f кг/ч" % (fm.hfm_volts(x), max(q, 0)),
                  old=1.0, new=1.0, time=w * st.dt)
        if w > 0:
            nd.lam, nd.lcmd, nd.err, nd.want = (acc[i][1] / w, acc[i][2] / w,
                                                acc[i][3] / w, acc[i][4] / w)
        if nd.time >= st.min_time and q > 0:
            # e > 1 -- воздуха больше, чем думает блок: таблицу вверх.
            # В терминах _limit «богаче» -- это множитель вверх.
            lo, hi = 1 - st.max_lean / 100, 1 + st.max_rich / 100
            nd.new = min(max(nd.want, lo), hi)
            hit = abs(nd.new - nd.want) > 1e-9
            nd.status = "упёрлось в предел шага" if hit else "по данным"
        else:
            nd.status = "мало данных" if w > 0 else "нет данных"
        res.nodes.append(nd)
    _fill(res.nodes, True, st.fill_empty, "one")

    mult = [nd.new for nd in res.nodes]
    raw = list(fm.hfm_raw)
    new = list(raw)
    top = (1 << 16) - 1
    for i in range(n):
        q = fm.hfm_flow(i)
        if q <= 0:
            continue
        c = interp1(xs, mult, i)
        v = round((q * c + fm.hfm_ofs) / fm.hfm_factor)
        new[i] = min(max(v, 0), top)
    # таблица обязана остаться неубывающей: иначе на подъёме напряжения
    # расход вдруг падает, и блок дёргает наполнение
    for i in range(1, n):
        if raw[i] >= raw[i - 1] and new[i] < new[i - 1]:
            new[i] = new[i - 1]
    res.hfm_new = new


def full_load_bands(samples: list[Sample], st: Settings,
                    step: float = 500.0) -> list[Band]:
    """Что показывал УДК на полном газе -- по полосам оборотов."""
    fl = [s for s in samples if s.fl]
    if not fl:
        return []
    lo = math.floor(min(s.rpm for s in fl) / step) * step
    hi = math.ceil(max(s.rpm for s in fl) / step) * step
    bands = []
    a = lo
    while a < hi:
        b = Band(a, a + step)
        inb = [s for s in fl if a <= s.rpm < a + step]
        if inb:
            b.time = len(inb) * st.dt
            b.lam = sum(s.lam for s in inb) / len(inb)
            b.lmin = min(s.lam for s in inb)
            b.lmax = max(s.lam for s in inb)
            b.lcmd = sum(s.lcmd for s in inb) / len(inb)
            b.err = sum(s.e for s in inb) / len(inb)
            bands.append(b)
        a += step
    return bands


def _model_warnings(fm: FuelModel, samples: list[Sample], st: Settings,
                    res: Result) -> None:
    k0, k1 = fm.kllamfa
    if k0 != k1:
        res.warnings.append(
            "KLLAMFA_0 и KLLAMFA_1 различаются; какой работает, решает блок "
            "(0xF87A). Считал по варианту %d, записываю в оба" % fm.variant)
    if fm.wdkvln[0] != fm.wdkvln[1]:
        res.warnings.append("WDKVLN_0 и WDKVLN_1 различаются; полная нагрузка "
                            "определялась по варианту %d" % fm.variant)
    cur = fm.kllamfa[fm.variant]
    if any(v > 1.0 for v in cur):
        res.notes.append(
            "KLLAMFA выше 1.000 (%s) не действует: блок берёт минимум с "
            "защитой компонентов, а она не выше 1.000. Фактическая цель на "
            "полном газе -- %.3f" % (
                "/".join("%.3f" % v for v in cur),
                min(min(cur), 1.0)))
    if not st.bts:
        amb = sum(1 for s in samples
                  if fm.kflbts_at(s.rpm, s.rl) < s.lcmd - 0.005)
        if amb:
            res.warnings.append(
                "%.1f с лога KFLBTS богаче расчётной цели: если защита "
                "компонентов успела включиться, блок хотел смесь богаче, чем "
                "я считаю. Проверьте с ключом «защита компонентов работала»"
                % (amb * st.dt))


def analyze(fm: FuelModel, ecu: logio.LogFile, wb: logio.LogFile | None,
            roles: dict, st: Settings) -> Result:
    res = Result(settings=st, target=st.target)
    fm.variant = st.variant
    samples = merge(fm, ecu, wb, roles, st, res)
    res.samples = samples
    _model_warnings(fm, samples, st, res)
    res.bands = full_load_bands(samples, st)
    if st.target == "hfm":
        correct_hfm(fm, samples, st, res)
    else:
        correct_kllamfa(fm, samples, st, res)
    return res


# -- запись в прошивку -------------------------------------------------------------

def build_edits(project, res: Result) -> list:
    """
    Правки для истории редактора: каждая отменяется, как ручная.

    Ничего не пишет в буфер сам -- применяет project.apply, как и всё
    остальное в редакторе.
    """
    from edits import CellEdit
    out = []
    if res.target == "hfm" and res.hfm_new:
        L = project.layout("HFM_LIN")
        cells = []
        for c in range(L.nx):
            old = M.get_raw(project.buf, L, 0, c)
            new = res.hfm_new[c]
            if new != old:
                cells.append((0, c, old, new))
        if cells:
            out.append(CellEdit("HFM_LIN", "УДК: тарировка ДМРВ", cells))
    elif res.target == "kllamfa" and res.kllamfa_new:
        for name in ("KLLAMFA_0", "KLLAMFA_1"):
            L = project.layout(name)
            cells = []
            for c, v in enumerate(res.kllamfa_new[:L.nx]):
                old = M.get_raw(project.buf, L, 0, c)
                new = M.phys_to_raw(L, v)
                # если узел не менялся, а старое было выше единицы --
                # не трогаем: значение то же по действию
                if new != old and abs(v - min(old * L.factor, 1.0)) > 1e-9:
                    cells.append((0, c, old, new))
            if cells:
                out.append(CellEdit(name, "УДК: полная нагрузка", cells))
    return out


def apply(project, res: Result) -> list:
    """Применить поправки к проекту. Возвращает применённые правки."""
    eds = build_edits(project, res)
    for ed in eds:
        project.apply(ed)
    return eds


# -- отчёт ---------------------------------------------------------------------------

def report(res: Result) -> str:
    st = res.settings
    L = []
    L.append("Смесь по УДК: %s" % ("тарировка ДМРВ (HFM_LIN)"
                                    if res.target == "hfm"
                                    else "лямбда полной нагрузки (KLLAMFA)"))
    L.append("Лог: %.0f с на общей шкале, годных %.0f с, в поправку %.0f с"
             % (res.total, res.accepted, len(res.used) * st.dt))
    rej = [(k, v) for k, v in res.rejected.items() if v > 0]
    if rej:
        L.append("Отброшено: " + "; ".join("%s %.0f с" % kv for kv in rej))
    for n in res.notes:
        L.append("* " + n)
    for w in res.warnings:
        L.append("! " + w)
    if res.bands:
        L.append("")
        L.append("Полная нагрузка, что показал УДК:")
        L.append("%-12s %7s %7s %7s %7s %8s %8s" % (
            "об/мин", "с", "λ ср", "λ мин", "λ макс", "цель", "ошибка"))
        for b in res.bands:
            L.append("%5.0f-%-6.0f %7.1f %7.3f %7.3f %7.3f %8.3f %+7.1f%%" % (
                b.lo, b.hi, b.time, b.lam, b.lmin, b.lmax, b.lcmd,
                (b.err - 1) * 100))
    L.append("")
    if res.target == "hfm":
        L.append("%-22s %6s %7s %7s %8s %8s  %s" % (
            "узел", "с", "λ УДК", "цель", "ошибка", "множ.", "откуда"))
        for n in res.nodes:
            if n.status == "нет данных" and n.new == n.old:
                continue
            L.append("%-22s %6.1f %7s %7s %8s %8.3f  %s" % (
                n.label, n.time,
                "%.3f" % n.lam if n.time else "-",
                "%.3f" % n.lcmd if n.time else "-",
                "%+.1f%%" % ((n.err - 1) * 100) if n.time else "-",
                n.new, n.status))
    else:
        L.append("KLLAMFA, желаемая λ на полном газе %.3f:" % st.lam_want)
        L.append("%-7s %6s %7s %7s %8s %7s %7s %8s  %s" % (
            "об/мин", "с", "λ УДК", "цель", "ошибка", "было", "стало",
            "изм.", "откуда"))
        for n in res.nodes:
            L.append("%-7s %6.1f %7s %7s %8s %7.3f %7.3f %+7.1f%%  %s" % (
                n.label, n.time,
                "%.3f" % n.lam if n.time else "-",
                "%.3f" % n.lcmd if n.time else "-",
                "%+.1f%%" % ((n.err - 1) * 100) if n.time else "-",
                n.old, n.new, n.change, n.status))
    return "\n".join(L)
