#!/usr/bin/env python3
"""
Смесь по УДК: лог езды -> поправки в прошивку.

Что считается
-------------
Блок не измеряет топливо, он его вычисляет:

    топливо = воздух_по_ДМРВ / (14.7 * лямбда_заданная) * коррекции

Отсюда одна величина на каждый момент лога -- ОШИБКА ВОЗДУХА e: во
сколько раз воздуха в цилиндре больше, чем насчитал блок. e = 1.10 --
воздуха на 10 % больше, топлива не хватает на те же 10 %.

Чем она меряется -- два способа, выбираются сами по логу.

* По УДК -- родному узкому зонду. Он говорит только «богаче единицы»
  или «беднее», но блок по нему сам держит лямбду 1.000, двигая быструю
  коррекцию STFT. Пока контур замкнут и зонд переключается, лямбда
  равна единице, и всю ошибку ДМРВ блок уже доливает коррекциями:

      e = (1 + STFT) * (1 + LTFT)

  У v2M цель на полном газе 1.000, и контур там тоже замкнут (0x84BB04,
  docs/41) -- ошибка видна и на верхах. Предел STFT в блоке +-25 %
  (0x84B06C: 0x15DD6 = 0xA000, нижний 0x1B0C2 = 0x6000). Упёрся --
  зонд остаётся бедным, и e по коррекциям -- только нижняя граница
  настоящей ошибки: поправка верна по знаку, но мала, нужен ещё проход.
* По ШДК -- широкополосному, если он есть. Он показывает лямбду
  числом, и контур не нужен:

      e = лямбда_ШДК * (1 + STFT) * (1 + LTFT) / лямбда_заданная

Чем править (выбирается)
------------------------
* ДМРВ, `HFM_LIN` -- тарировка: расход по напряжению. Поправка e
  раскладывается по расходу, и таблица умножается на неё. Чинит причину
  (неверно измеренный воздух) сразу во всех режимах, и нагрузка для УОЗ
  становится честной. Это классическая «откатка ДМРВ по УДК».
* Полная нагрузка, `KLLAMFA_0/_1` -- цель по лямбде при газе в пол, по
  оборотам. Только с ШДК: цель, отличная от 1.000, размыкает контур, и
  узкому зонду проверить её нечем. Новая цель такая, чтобы ШДК показал
  желаемую лямбду:

      цель_новая = лямбда_желаемая * цель_была / лямбда_ШДК_без_STFT

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

# (роль, подпись, обязательна). STFT обязательна без ШДК -- это
# проверяется при сведении: роль сама по себе необязательна
ROLES = [
    ("rpm", "Обороты", True),
    ("maf", "Расход воздуха (ДМРВ)", True),
    ("tps", "Дроссель", True),
    ("stft", "Быстрая коррекция (STFT)", False),
    ("ltft", "Адаптация (LTFT)", False),
    ("o2v", "УДК: узкий зонд, В", False),
    ("closed", "Контур по зонду замкнут", False),
    ("lam", "ШДК: лямбда или AFR, если есть", False),
    ("ect", "Температура ОЖ", False),
    ("adc", "АЦП ДМРВ (точный ключ тарировки)", False),
    ("lcmd", "Заданная лямбда из блока", False),
]
ROLE_TITLES = {r: t for r, t, _ in ROLES}

_PAT = {
    "rpm": (r"rpm|об/мин|оборот|engine speed|nmot|частота вращ", r"vehicle|скорост|км/ч|km/h"),
    "maf": (r"\bmaf\b|mass air|air ?flow|air mass|расход возд|массов\w* расход|дмрв|\bml\b",
            r"volt|напряж|\(v\)|, ?в$|voltage|correct|коррек|adc|ацп|counts"),
    "adc": (r"(maf|дмрв|hfm).*(adc|ацп)|(adc|ацп).*(maf|дмрв|hfm)", r"$^"),
    "lcmd": (r"lambda target|target lambda|заданн\w* (λ|лямбд)|"
             r"цел\w* (λ|лямбд)", r"command|equivalence"),
    "tps": (r"throttle|дросс|\btps\b|\bдз\b",
            r"relative|относит|command|задан|педал|pedal|accel|\bb\)|\bb$|volt|напряж"),
    # ШДК. Слово «УДК» само по себе сюда не ведёт: УДК -- родной узкий
    # зонд, у него вольты. «УДК лямбда» из логов прошлой версии логгера
    # -- ШДК и находится по «лямбде»
    "lam": (r"lambda|лямбд|λ|\bшдк\b|\bafr\b|wideband|\blc-?[12]\b|wbo2|air.?fuel|соста\w+ смеси",
            r"command|задан|equivalence|o2 sensor|кислород|volt|напряж|target|цел|"
            r"control|active|closed|регулят|контур|зонд"),
    "o2v": (r"(\bo2\b|oxygen|кислород|\bудк\b|зонд).*(volt|напряж|\(v\)|\bv$|, ?в$)|"
            r"(volt|напряж).*(\bo2\b|oxygen|кислород|\bудк\b|зонд)",
            r"sensor ?2|датчик ?2|зонд ?2|\bs2\b|b1s2|bank ?2|банк ?2|после|"
            r"downstream|post|диагност|trim|коррек"),
    "closed": (r"closed.?loop|fuel system status|топливн\w* систем|"
               r"контур|регулятор по зонду|lambda control",
               r"bank ?2|банк ?2"),
    "stft": (r"short.?term|\bstft\b|кратко|быстр\w* корр", r"bank ?2|банк ?2"),
    "ltft": (r"long.?term|\bltft\b|долго|адаптац", r"bank ?2|банк ?2"),
    "ect": (r"coolant|охлажд|\bож\b|\bтож\b|engine temp|температура двиг|\bect\b",
            r"\bvolt"),
}


def detect_roles(ecu: logio.LogFile, wb: logio.LogFile | None) -> dict:
    """
    Какой канал за что отвечает -- по имени и единицам.

    Возвращает роль -> (источник, имя канала) или None. ШДК ищется
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
        if role == "maf" and len(cands) > 1:
            # расход из режима 22 (0.1 кг/ч) точнее OBD-шного (0.25 г/с)
            ecu_first = [c for c in cands if re.search(r"ecu|эбу", c[1], re.I)]
            cands = ecu_first or cands
        if role == "closed" and len(cands) > 1:
            # флаг регулятора (DID 1216) надёжнее PID 03: тот бывает
            # устаревшим (0x84B89E)
            act = [c for c in cands
                   if re.search(r"active|регулятор", c[1], re.I)]
            cands = act or cands
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
    target: str = "hfm"         # "hfm" | "kllamfa"
    # желаемая лямбда на полном газе; None -- та, что уже задана прошивкой
    # (у v2M и MPower это 1.000): смесь доводится до цели, цель не меняется
    lam_want: float | None = None
    lag: float = 0.3            # задержка ШДК относительно ЭБУ, с
    offset: float = 0.0         # время ЭБУ = время ШДК + offset (отдельный лог)
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
    min_time: float = 1.0       # с данных на узел
    max_rich: float = 25.0      # % обогащения за один проход
    max_lean: float = 10.0      # % обеднения за один проход
    fill_empty: bool = True     # продлить на узлы без данных
    bts: bool = False           # защита компонентов работала полностью
    lam_kind: str = "auto"      # "auto" | "lambda" | "afr"
    maf_unit: str = "auto"      # "auto" | "g/s" | "kg/h"
    stoich: float = STOICH
    variant: int = 0
    hfm_step: int = 16          # точек тарировки на узел поправки
    # без ШДК -- по узкому зонду и коррекциям
    stft_lim: float = 25.0      # предел STFT в блоке, % (0x15DD6, 0x1B0C2)
    nb_settle: float = 3.0      # с после замыкания контура: STFT догоняет
    open_hold: float = 3.0      # STFT ровно 0 дольше -- контур разомкнут


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
    adc: float | None = None   # АЦП ДМРВ, если блок его отдал
    o2v: float | None = None   # узкий зонд, В
    sat: int = 0        # STFT в упоре: +1 -- сверху (бедно), -1 -- снизу
    closed: bool = True  # контур по зонду замкнут


@dataclass
class Node:
    x: float            # обороты или номер точки тарировки
    label: str
    time: float = 0.0   # с данных (взвешенных)
    lam: float = 0.0    # средняя лямбда ШДК
    lcmd: float = 0.0   # средняя заданная
    err: float = 0.0    # средняя ошибка воздуха e
    want: float = 0.0   # что требуют данные (до пределов)
    sat: float = 0.0    # доля времени со STFT в упоре
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
    # без ШДК
    stft: float = 0.0
    ltft: float = 0.0
    sat: float = 0.0        # доля времени замкнутого контура в упоре STFT
    closed: float = 0.0     # доля времени полной нагрузки с замкнутым контуром
    o2lean: float | None = None  # доля разомкнутого времени с бедным зондом


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
    narrow: bool = False        # без ШДК: по узкому зонду и коррекциям
    open_fl: list = field(default_factory=list)  # полный газ, контур разомкнут

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


def _transients(tps: logio.Channel, st: Settings,
                rpm: logio.Channel | None = None) -> list[tuple[float, float]]:
    """
    Отрезки времени, когда дроссель дёргали: туда не смотрим.

    Два случая. Рывок -- дроссель меняется быстрее tps_rate. И выход из
    наката -- дроссель открыли после закрытого на оборотах, с какой угодно
    скоростью: топливо возвращается после сброса, плёнка на стенках
    впуска пустая, и первые мгновения смесь бедная не из-за ДМРВ. ELM
    опрашивает дроссель пару раз в секунду, и плавное на вид открытие с
    нуля до 16 % за полсекунды по скорости рывком не считается -- поэтому
    отдельное правило.
    """
    out = []
    t, v = tps.t, tps.v
    for i in range(1, len(t)):
        span = t[i] - t[i - 1]
        if span <= 0 or span > 2 * st.max_gap:
            continue
        if abs(v[i] - v[i - 1]) / span > st.tps_rate:
            out.append((t[i - 1], t[i] + st.settle))
            continue
        closed_a, closed_b = v[i - 1] < st.tps_min, v[i] < st.tps_min
        if closed_a != closed_b:
            r = (logio.sample(rpm, t[i - 1], st.max_gap)
                 if rpm is not None else None)
            if r is None or r > st.cut_rpm:
                out.append((t[i - 1], t[i] + st.settle))
    return _merge_spans(out)


def _inside(spans, starts, t: float) -> bool:
    i = bisect.bisect_right(starts, t) - 1
    return i >= 0 and spans[i][0] <= t <= spans[i][1]


def _reject(res: Result, why: str, dt: float) -> None:
    res.rejected[why] = res.rejected.get(why, 0.0) + dt


REASONS = ("нет показаний", "холодный мотор", "обороты ниже порога",
           "дроссель закрыт, сброс топлива", "ШДК вне диапазона",
           "рывок дросселя", "нет расхода", "контур разомкнут",
           "контур только что замкнулся")


def _merge_spans(out: list) -> list:
    out.sort()
    merged = []
    for a, b in out:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def _around(ts, idx: list, max_gap: float) -> tuple:
    """
    Отрезок от показания перед idx[0] до показания после idx[-1]: где-то
    между ними состояние и сменилось, и верить нельзя всему промежутку.
    """
    i, j = idx[0], idx[-1]
    a = ts[i - 1] if i > 0 and ts[i] - ts[i - 1] <= 2 * max_gap else ts[i]
    b = (ts[j + 1] if j + 1 < len(ts) and ts[j + 1] - ts[j] <= 2 * max_gap
         else ts[j])
    return a, b


def _stft_open(ch: logio.Channel, st: Settings,
               o2: logio.Channel | None = None) -> list:
    """
    Где STFT стоит ровно на нуле -- там контур разомкнут.

    Выключая регулятор, блок сбрасывает STFT в 0x8000, то есть в 0 %
    (0x84A9B6), и держит там. Это ловит и разомкнутый контур, о котором
    PID 03 промолчал (байт 0x8939 бывает устаревшим).

    Но и замкнутый контур на здоровом режиме ходит пилой вокруг нуля, а
    OBD огрубляет STFT до 0.78 %: показание «ровно 0» там обычное. Хуже
    того, ELM спрашивает раз в секунду, «качели» качаются примерно так
    же, и показания могут раз за разом попадать в одну фазу -- как
    стробоскоп. Поэтому нужно долго -- open_hold и не меньше четырёх
    показаний подряд, -- а если в логе есть УДК, он за это время не
    должен переключаться туда-сюда: в разомкнутом контуре смесь не
    качается. Переключения бывают и там -- сменился режим, обогащение на
    разгон, -- но редко; «качели» же меняют сторону через показание.
    """
    out, run = [], []
    ts = ch.t

    def steady(a, b):
        if o2 is None:
            return True
        i = bisect.bisect_left(o2.t, a)
        j = bisect.bisect_right(o2.t, b)
        side = [v > 0.45 for v in o2.v[i:j]]
        flips = sum(1 for x, y in zip(side, side[1:]) if x != y)
        return flips < 3 or flips < 0.35 * (len(side) - 1)

    def flush():
        if (len(run) >= 4 and ts[run[-1]] - ts[run[0]] >= st.open_hold
                and steady(ts[run[0]], ts[run[-1]])):
            out.append(_around(ts, run, st.max_gap))
        run.clear()

    for i, v in enumerate(ch.v):
        if abs(v) < 0.05:
            if run and ts[i] - ts[run[-1]] > 2 * st.max_gap:
                flush()
            run.append(i)
        else:
            flush()
    flush()
    return _merge_spans(out)


def _closed_kind(ch: logio.Channel) -> str:
    """Флаг 0/1 (DID 1216) или состояние по OBD (PID 03: 2 -- замкнут)."""
    lab = ("%s %s" % (ch.name, ch.unit)).lower()
    if re.search(r"status|состоя|систем", lab):
        return "status"
    if re.search(r"active|регулятор|closed", lab):
        return "flag"
    return "status" if any(v > 1.5 for v in ch.v) else "flag"


def _is_closed(v: float, kind: str) -> bool:
    if kind == "flag":
        return v >= 0.5
    n = int(round(v))
    if n > 255:              # оба байта PID 03 одним числом
        n >>= 8
    return bool(n & 2)


def _channel_open(ch: logio.Channel, st: Settings) -> list:
    """
    Отрезки, где канал «контур замкнут» говорит «нет»: от середины до
    соседнего показания до середины до следующего. Флаг спрашивается раз в
    несколько кругов, и брать весь промежуток до соседа -- значит терять
    секунды полного газа перед каждым накатом. Сам накат и так ловится
    точно, по дросселю.
    """
    kind = _closed_kind(ch)
    ts = ch.t
    out = []
    for i, v in enumerate(ch.v):
        if _is_closed(v, kind):
            continue
        a = b = ts[i]
        if i > 0 and ts[i] - ts[i - 1] <= 2 * st.max_gap:
            a = (ts[i - 1] + ts[i]) / 2
        if i + 1 < len(ts) and ts[i + 1] - ts[i] <= 2 * st.max_gap:
            b = (ts[i] + ts[i + 1]) / 2
        out.append((a, b))
    return _merge_spans(out)


def merge(fm: FuelModel, ecu: logio.LogFile, wb: logio.LogFile | None,
          roles: dict, st: Settings, res: Result) -> list[Sample]:
    """Свести каналы на общую шкалу, отбраковать, посчитать e."""
    ch = {r: _channel(ecu, wb, roles.get(r)) for r, _, _ in ROLES}
    need = [ROLE_TITLES[r] for r, _, must in ROLES if must and ch[r] is None]
    if need:
        raise ValueError("в логе не найдено: " + ", ".join(need))
    narrow = ch["lam"] is None
    res.narrow = narrow
    if narrow and ch["stft"] is None:
        raise ValueError(
            "в логе нет ни ШДК, ни быстрой коррекции STFT. По родному зонду "
            "(УДК) смесь считается через коррекции: он сам говорит только "
            "«богато» или «бедно». Добавьте в лог Short term fuel trim "
            "(PID 06 или 14)")

    maf_k, maf_unit = maf_scale(ch["maf"], st.maf_unit)
    if narrow:
        res.notes.append(
            "ШДК нет -- считаю по УДК (узкий зонд) и коррекциям: пока контур "
            "замкнут, блок держит λ 1.000, и ошибка ДМРВ -- это то, что "
            "доливают STFT и LTFT")
        if ch["o2v"] is not None:
            res.notes.append("узкий зонд «%s»: по нему проверяется упор STFT"
                             % ch["o2v"].name)
        if ch["closed"] is not None:
            res.notes.append("замкнут ли контур -- по «%s» и по цели из "
                             "прошивки%s" % (
                                 ch["closed"].name,
                                 "" if _closed_kind(ch["closed"]) == "flag"
                                 else ", и по STFT (ровно 0 дольше %.0f с -- "
                                 "разомкнут)" % st.open_hold))
        else:
            res.notes.append("замкнут ли контур -- по цели из прошивки и по "
                             "STFT: блок, размыкая, сбрасывает его в 0 (ровно "
                             "0 дольше %.0f с -- разомкнут)" % st.open_hold)
        lam_k = 1.0
        lam_ch = None
    else:
        lam_k, lam_kind = lam_scale(ch["lam"], st.lam_kind, st.stoich)
        res.notes.append("ШДК: %s «%s»%s" % (
            lam_kind, ch["lam"].name,
            (", делю на %.1f" % st.stoich) if lam_kind == "AFR" else ""))
        # лямбда: из своего лога на часы ЭБУ, и раньше на время задержки --
        # ШДК видит газ, который сгорел lag секунд назад
        shift = -st.lag
        if roles["lam"][0] == "wb":
            shift += st.offset
        lam_ch = logio.shifted(ch["lam"], shift)
    res.notes.append("расход: «%s» в %s" % (ch["maf"].name, maf_unit))

    for r in ("stft", "ltft", "ect"):
        if ch[r] is None:
            res.warnings.append("%s в логе нет" % ROLE_TITLES[r])
    if narrow and ch["ltft"] is None:
        res.warnings.append(
            "без LTFT считаю адаптацию нулевой: если блок её уже выучил, "
            "эта часть ошибки в поправку не попадёт")
    if ch["adc"] is not None:
        res.notes.append("ключ тарировки ДМРВ -- сырой АЦП «%s», без "
                         "поправки пульсаций" % ch["adc"].name)
    if ch["lcmd"] is not None:
        res.notes.append("заданная лямбда -- из блока, «%s», а не из модели "
                         "прошивки" % ch["lcmd"].name)

    spans = _transients(ch["tps"], st, ch["rpm"])
    starts = [a for a, _ in spans]
    if narrow:
        opens = []
        # флаг регулятора из режима 22 -- сам блок, ему и верим; PID 03
        # бывает устаревшим, к нему -- ещё и STFT, стоящий на нуле
        flag = ch["closed"] is not None and _closed_kind(ch["closed"]) == "flag"
        if not flag:
            opens = _stft_open(ch["stft"], st, ch["o2v"])
        if ch["closed"] is not None:
            opens = _merge_spans(opens + _channel_open(ch["closed"], st))
        open_starts = [a for a, _ in opens]
    last_open = -1e9

    main = lam_ch if lam_ch is not None else ch["stft"]
    t0 = max(ch["rpm"].t[0], ch["maf"].t[0], ch["tps"].t[0], main.t[0])
    t1 = min(ch["rpm"].t[-1], ch["maf"].t[-1], ch["tps"].t[-1], main.t[-1])
    if t1 <= t0:
        raise ValueError("лог ЭБУ и лог ШДК не пересекаются по времени -- "
                         "проверьте сдвиг" if not narrow else
                         "в логе нет отрезка, где есть все нужные каналы")
    for why in REASONS:
        if narrow and why == "ШДК вне диапазона":
            continue
        if not narrow and why.startswith("контур"):
            continue
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
        lam = logio.sample(lam_ch, t, g) if lam_ch is not None else 1.0
        stft = (logio.sample(ch["stft"], t, g)
                if ch["stft"] is not None else None)
        if None in (rpm, maf, tps, lam) or (narrow and stft is None):
            _reject(res, "нет показаний", st.dt)
            continue
        maf *= maf_k
        lam *= lam_k
        rl = fm.rl(rpm, maf)
        lcmd, fl = fm.lam_cmd(rpm, tps, rl, st.bts)
        if ch["lcmd"] is not None:
            # блок сам сказал, чего хотел (чтение памяти 0x9818) -- это
            # точнее любой модели: учтены и защита компонентов, и переходы
            got = logio.sample(ch["lcmd"], t, g)
            if got is not None and 0.5 < got < 1.5:
                lcmd = got
        ect = (logio.sample(ch["ect"], t, 30.0)
               if ch["ect"] is not None else None)
        cold = ect is not None and ect < st.ect_min
        cut = tps < st.tps_min and rpm > st.cut_rpm
        is_open = False
        if narrow:
            # регулятор выключают цель не ровно 1.000 и нагрузка от 99.75 %
            # (0x84BB04), сброс топлива и холодный мотор; плюс что сказал
            # сам блок и STFT, сброшенный в ноль
            is_open = (cold or cut or abs(lcmd - 1.0) > 0.002 or rl >= 99.75
                       or (opens and _inside(opens, open_starts, t)))
            if is_open:
                last_open = t
        if cold:
            _reject(res, "холодный мотор", st.dt)
            continue
        if rpm < st.rpm_min:
            _reject(res, "обороты ниже порога", st.dt)
            continue
        if cut:
            # закрытый дроссель выше ХХ -- накат, топливо перекрыто или
            # вот-вот вернётся; на самом ХХ закрытый дроссель -- норма
            _reject(res, "дроссель закрыт, сброс топлива", st.dt)
            continue
        if not narrow and not (st.lam_lo <= lam <= st.lam_hi):
            _reject(res, "ШДК вне диапазона", st.dt)
            continue
        if spans and _inside(spans, starts, t):
            _reject(res, "рывок дросселя", st.dt)
            continue
        if maf <= 0.5:
            _reject(res, "нет расхода", st.dt)
            continue
        # LTFT меняется минутами, и логгер спрашивает её редко: дыра
        # между показаниями -- не повод считать её нулём
        ltft = (logio.sample(ch["ltft"], t, 30.0)
                if ch["ltft"] is not None else None)
        adc = (logio.sample(ch["adc"], t, g)
               if ch["adc"] is not None else None)
        o2v = (logio.sample(ch["o2v"], t, g)
               if ch["o2v"] is not None else None)
        sat = 0
        if narrow:
            if is_open:
                if fl:
                    res.open_fl.append(Sample(
                        t, rpm, maf, tps, lcmd, stft, ltft, fl, lcmd, rl,
                        1.0, adc, o2v, 0, False))
                _reject(res, "контур разомкнут", st.dt)
                continue
            if t - last_open < st.nb_settle:
                # STFT после замыкания стартует с нуля и догоняет ошибку
                # интегратором -- несколько секунд он врёт
                _reject(res, "контур только что замкнулся", st.dt)
                continue
            # контур замкнут, зонд переключается -- в цилиндре цель
            lam = lcmd
            edge = st.stft_lim - 1.0
            if stft >= edge and (o2v is None or o2v < 0.45):
                sat = 1
            elif stft <= -edge and (o2v is None or o2v > 0.45):
                sat = -1
        trims = (1 + (stft or 0.0) / 100) * (1 + (ltft or 0.0) / 100)
        out.append(Sample(t, rpm, maf, tps, lam, stft, ltft, fl, lcmd, rl,
                          lam * trims / lcmd, adc, o2v, sat))
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
    Сдвиг отдельного лога ШДК: время ЭБУ = время ШДК + сдвиг.

    Опора -- КОНЕЦ сброса топлива. Катились накатом с закрытым дросселем
    -- форсунки перекрыты, ШДК лежит в бедном упоре. Нажали газ -- топливо
    вернулось сразу, и ШДК падает к единице. В логе ЭБУ это виден подъём
    дросселя после закрытого на оборотах, в логе ШДК -- падение лямбды из
    упора. Таких событий за поездку десятки, и совпасть все разом при
    неверном сдвиге не могут.

    Начало сброса для этого не годится: Bosch перекрывает топливо не
    сразу, а с выдержкой, и она сместила бы сдвиг на неё. Возврат топлива
    по газу выдержки не имеет, между событиями только задержка самого ШДК.

    Если событий мало, а у обоих логов есть часы, берём разницу часов.
    """
    rpm = _channel(ecu, None, roles.get("rpm"))
    tps = _channel(ecu, None, roles.get("tps"))
    lam = _channel(ecu, wb, roles.get("lam"))
    if rpm is None or tps is None or lam is None or roles["lam"][0] != "wb":
        return None, "нужен лог ЭБУ с оборотами и дросселем и отдельный лог ШДК"
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
    # события лога ЭБУ на шкале ШДК сдвинуты ещё и на задержку датчика
    ev_ecu = [x + st.lag for x in ev_ecu]

    clock = None
    if ecu.t0 is not None and wb.t0 is not None:
        if ecu.t0_kind == wb.t0_kind:
            clock = wb.t0 - ecu.t0
        elif {ecu.t0_kind, wb.t0_kind} == {"day", "epoch"}:
            # у одного дата и время, у другого только время: сравниваем
            # время суток (даты в «epoch» -- местные, как в логах)
            def sod(log):
                return log.t0 % 86400 if log.t0_kind == "epoch" else log.t0
            clock = sod(wb) - sod(ecu)
            if clock > 43200:
                clock -= 86400
            elif clock < -43200:
                clock += 86400

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
    return None, ("опорных событий мало (ЭБУ %d, ШДК %d), часов нет -- "
                  "сдвиг задайте руками" % (len(ev_ecu), len(ev_wb)))


# -- поправки -------------------------------------------------------------------

def _accumulate(xs, samples, key, value):
    """
    Взвешенные суммы по узлам: время, лямбда, цель, ошибка, требование,
    время в упоре STFT.
    """
    acc = [[0.0] * 6 for _ in xs]
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
            a[5] += w * (1.0 if s.sat else 0.0)
    return acc


def _fill(nodes: list[Node], interior: bool, above: bool, below: str) -> None:
    """
    Узлы без данных. Между узлами с данными -- линейно, выше последнего --
    как у последнего (если разрешено), ниже первого -- `below`:
    "hold" -- как у первого, "one" -- без изменения.
    """
    have = [i for i, n in enumerate(nodes) if n.status == "по данным"
            or n.status.startswith("упёрлось") or n.status.startswith("упор")]
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


SAT_SHARE = 0.3     # столько времени узла STFT в упоре -- узел в упоре


def _sat_warning(res: Result, st: Settings) -> None:
    hit = [n for n in res.nodes if n.time and n.sat >= SAT_SHARE]
    if not hit:
        return
    res.warnings.append(
        "на %d узл. (%s) STFT стоял в упоре %.0f %%: зонд так и не дошёл до "
        "единицы, настоящая ошибка больше, чем видно по коррекциям. "
        "Поправка там -- нижняя граница: залейте, снимите новый лог и "
        "посчитайте ещё раз, каждый проход добирает остаток"
        % (len(hit), ", ".join(n.label.split(",")[0] for n in hit[:6])
           + (" …" if len(hit) > 6 else ""), st.stft_lim))


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
        # цель, при которой ШДК покажет желаемое; STFT на полном газе
        # выключается вместе с переходом на богатую цель, его вклад убираем
        want = s.lcmd if st.lam_want is None else st.lam_want
        return want * s.lcmd / (s.lam * (1 + (s.stft or 0.0) / 100))

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
            n.sat = acc[i][5] / w
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
    def key(s):
        # АЦП -- точное место в таблице (индекс = АЦП / 2, docs/36); без
        # него -- обратный поиск по расходу, сдвинутый на поправку пульсаций
        if s.adc is not None:
            # блок берёт HFM_LIN[АЦП >> 1] без интерполяции (0x842C2A):
            # нечётный отсчёт относится к той же точке, что и чётный
            return min(max(float(int(s.adc) >> 1), 0.0), float(n - 1))
        return fm.hfm_index(s.maf)
    acc = _accumulate(xs, used, key, lambda s: s.e)
    for i, x in enumerate(xs):
        w = acc[i][0]
        q = fm.hfm_flow(x)
        nd = Node(x=x, label="%.2f В, %.0f кг/ч" % (fm.hfm_volts(x), max(q, 0)),
                  old=1.0, new=1.0, time=w * st.dt)
        if w > 0:
            nd.lam, nd.lcmd, nd.err, nd.want = (acc[i][1] / w, acc[i][2] / w,
                                                acc[i][3] / w, acc[i][4] / w)
            nd.sat = acc[i][5] / w
        if nd.time >= st.min_time and q > 0:
            # e > 1 -- воздуха больше, чем думает блок: таблицу вверх.
            # В терминах _limit «богаче» -- это множитель вверх.
            lo, hi = 1 - st.max_lean / 100, 1 + st.max_rich / 100
            nd.new = min(max(nd.want, lo), hi)
            hit = abs(nd.new - nd.want) > 1e-9
            if nd.sat >= SAT_SHARE:
                # коррекция в упоре, зонд всё равно беден (или богат):
                # настоящая ошибка больше -- поправка верна по знаку, но
                # это нижняя граница
                nd.status = "упор STFT, нужен ещё проход"
            else:
                nd.status = "упёрлось в предел шага" if hit else "по данным"
        else:
            nd.status = "мало данных" if w > 0 else "нет данных"
        res.nodes.append(nd)
    _fill(res.nodes, True, st.fill_empty, "one")
    _sat_warning(res, st)

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
                    step: float = 500.0, open_fl=()) -> list[Band]:
    """
    Полная нагрузка по полосам оборотов: что показал ШДК, а без него --
    коррекции, упор STFT и сколько времени контур был замкнут.
    """
    fl = [s for s in samples if s.fl] + list(open_fl)
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
            cl = [s for s in inb if s.closed]
            b.closed = len(cl) / len(inb)
            if cl:
                k = len(cl)
                b.lam = sum(s.lam for s in cl) / k
                b.lmin = min(s.lam for s in cl)
                b.lmax = max(s.lam for s in cl)
                b.lcmd = sum(s.lcmd for s in cl) / k
                b.err = sum(s.e for s in cl) / k
                b.stft = sum(s.stft or 0.0 for s in cl) / k
                b.ltft = sum(s.ltft or 0.0 for s in cl) / k
                b.sat = sum(1 for s in cl if s.sat) / k
            op = [s for s in inb if not s.closed and s.o2v is not None]
            if op:
                b.o2lean = sum(1 for s in op if s.o2v < 0.45) / len(op)
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


def _open_warning(samples: list[Sample], st: Settings, res: Result) -> None:
    """Без ШДК полный газ виден, только пока контур замкнут."""
    op = len(res.open_fl) * st.dt
    cl = sum(1 for s in samples if s.fl) * st.dt
    if op < max(2.0, cl):
        return
    msg = ("на полном газе контур был разомкнут %.0f с, замкнут %.0f с: там "
           "блок по зонду не регулирует, и ошибку по коррекциям не видно"
           % (op, cl))
    nb = [s for s in res.open_fl if s.o2v is not None]
    if nb:
        msg += ("; зонд при этом показывал «бедно» %.0f %% времени"
                % (100.0 * sum(1 for s in nb if s.o2v < 0.45) / len(nb)))
    msg += (". Если KLLAMFA ниже 1.000 -- поставьте на время 1.000 (как в "
            "v2M): контур замкнётся и на верхах, и ошибка ДМРВ там станет "
            "видна в STFT")
    res.warnings.append(msg)


def analyze(fm: FuelModel, ecu: logio.LogFile, wb: logio.LogFile | None,
            roles: dict, st: Settings) -> Result:
    res = Result(settings=st, target=st.target)
    fm.variant = st.variant
    samples = merge(fm, ecu, wb, roles, st, res)
    if res.narrow and st.target == "kllamfa":
        raise ValueError(
            "цель полного газа (KLLAMFA) без ШДК не поправить: как только "
            "цель уходит от 1.000, блок размыкает контур, и узкий зонд "
            "показывает только «богато» или «бедно», без числа. По УДК "
            "правится ДМРВ (HFM_LIN) -- он чинит и верха")
    res.samples = samples
    _model_warnings(fm, samples, st, res)
    res.bands = full_load_bands(samples, st, open_fl=res.open_fl)
    if res.narrow:
        _open_warning(samples, st, res)
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
    L.append("Смесь %s: %s" % (
        "по УДК (узкий зонд, по коррекциям)" if res.narrow else "по ШДК",
        "тарировка ДМРВ (HFM_LIN)" if res.target == "hfm"
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
    if res.bands and res.narrow:
        L.append("")
        L.append("Полная нагрузка по коррекциям (ошибка -- сколько воздуха "
                 "блок недосчитал):")
        L.append("%-12s %6s %7s %7s %7s %8s %6s %10s" % (
            "об/мин", "с", "замкн.", "STFT", "LTFT", "ошибка", "упор",
            "зонд беден"))
        for b in res.bands:
            has = b.closed > 0
            L.append("%5.0f-%-6.0f %6.1f %6.0f%% %7s %7s %8s %6s %10s" % (
                b.lo, b.hi, b.time, b.closed * 100,
                "%+.1f%%" % b.stft if has else "-",
                "%+.1f%%" % b.ltft if has else "-",
                "%+.1f%%" % ((b.err - 1) * 100) if has else "-",
                "%.0f%%" % (b.sat * 100) if has else "-",
                "%.0f%%" % (b.o2lean * 100) if b.o2lean is not None else "-"))
        L.append("  замкн. -- доля времени с работающим регулятором: только "
                 "по ней видна ошибка; упор -- STFT на пределе, ошибка там "
                 "больше показанной; зонд беден -- при разомкнутом контуре")
    elif res.bands:
        L.append("")
        L.append("Полная нагрузка, что показал ШДК:")
        L.append("%-12s %7s %7s %7s %7s %8s %8s" % (
            "об/мин", "с", "λ ср", "λ мин", "λ макс", "цель", "ошибка"))
        for b in res.bands:
            L.append("%5.0f-%-6.0f %7.1f %7.3f %7.3f %7.3f %8.3f %+7.1f%%" % (
                b.lo, b.hi, b.time, b.lam, b.lmin, b.lmax, b.lcmd,
                (b.err - 1) * 100))
    L.append("")
    if res.target == "hfm" and res.narrow:
        L.append("%-22s %6s %8s %6s %8s  %s" % (
            "узел", "с", "ошибка", "упор", "множ.", "откуда"))
        for n in res.nodes:
            if n.status == "нет данных" and n.new == n.old:
                continue
            L.append("%-22s %6.1f %8s %6s %8.3f  %s" % (
                n.label, n.time,
                "%+.1f%%" % ((n.err - 1) * 100) if n.time else "-",
                "%.0f%%" % (n.sat * 100) if n.time else "-",
                n.new, n.status))
    elif res.target == "hfm":
        L.append("%-22s %6s %7s %7s %8s %8s  %s" % (
            "узел", "с", "λ ШДК", "цель", "ошибка", "множ.", "откуда"))
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
        L.append("KLLAMFA, желаемая λ на полном газе %s:" % (
            "как в прошивке" if st.lam_want is None else "%.3f" % st.lam_want))
        L.append("%-7s %6s %7s %7s %8s %7s %7s %8s  %s" % (
            "об/мин", "с", "λ ШДК", "цель", "ошибка", "было", "стало",
            "изм.", "откуда"))
        for n in res.nodes:
            L.append("%-7s %6.1f %7s %7s %8s %7.3f %7.3f %+7.1f%%  %s" % (
                n.label, n.time,
                "%.3f" % n.lam if n.time else "-",
                "%.3f" % n.lcmd if n.time else "-",
                "%+.1f%%" % ((n.err - 1) * 100) if n.time else "-",
                n.old, n.new, n.change, n.status))
    return "\n".join(L)
