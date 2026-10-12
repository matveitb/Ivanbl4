#!/usr/bin/env python3
"""
Модель мотора с заведомо известной ошибкой ДМРВ -- для проверки udk.py.

Проверять «смесь по УДК» на настоящем логе нечем: правильного ответа к
нему никто не знает. Поэтому здесь мотор, у которого ответ известен:

* физический ДМРВ даёт напряжение по ИСХОДНОЙ тарировке, но врёт на
  err(расход) -- например, недодаёт 15 % на больших расходах;
* блок считает воздух по ТЕКУЩЕЙ тарировке и цель по ТЕКУЩИМ картам --
  ровно как FuelModel, то есть как блок по коду;
* работает регулятор по узкому зонду (УДК): STFT подтягивает смесь к
  единице, LTFT стоит на своём; выключается -- как принято думать, на
  полной нагрузке, или как по коду блока (by_code); качает STFT плавно
  или «качелями», как настоящий (nb_ctrl);
* при отпускании газа на оборотах топливо перекрывается, ШДК уходит в
  бедный упор; при рывке газа -- короткое обогащение на ускорение;
* ELM опрашивает параметры по кругу, около раза в секунду каждый, с
  квантованием OBD; ШДК пишет свой лог 10 раз в секунду, со своим нулём
  часов и с задержкой датчика.

Тогда проверка честная: udk.py обязан найти заложенную ошибку, а та же
модель с исправленной прошивкой -- выдать желаемую смесь.

Модуль нужен проверкам и самопроверке сборки, в работе не участвует.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

# (длительность с, обороты в начале, в конце, дроссель %)
DRIVE = [
    (30, 850, 850, 2.0),             # прогрев на ХХ
    (12, 2000, 2000, 14), (12, 2500, 2500, 18), (12, 3000, 3000, 22),
    (10, 3500, 3500, 26), (10, 4000, 4000, 30), (8, 4500, 4500, 34),
    (6, 1500, 6500, 100), (4, 6500, 2000, 0),      # полный газ и накат
    (10, 2200, 2200, 16),
    (7, 1600, 6500, 100), (4, 6500, 2200, 0),
    (10, 2800, 2800, 22),
    (7, 1500, 6600, 100), (4, 6600, 2000, 0),
    (8, 3200, 3200, 45), (8, 2400, 2400, 60),
    (6, 2000, 6200, 100), (5, 6200, 1800, 0),
    (15, 2600, 2600, 20),
]

PIDS = [("Engine RPM", "rpm"), ("Mass air flow rate", "g/s"),
        ("Throttle position", "%"), ("Short term fuel % trim - Bank 1", "%"),
        ("Long term fuel % trim - Bank 1", "%"),
        ("Engine coolant temperature", "°C")]
# лог без ШДК: тот же Car Scanner плюс напряжение родного зонда
PIDS_NB = PIDS + [("O2 sensor 1 voltage", "V")]


def err_lean_top(q: float) -> float:
    """По умолчанию: ДМРВ недодаёт до 18 % выше 120 кг/ч."""
    return 1.0 + 0.18 * min(1.0, max(0.0, (q - 120.0) / 230.0))


@dataclass
class Truth:
    t: float
    rpm: float
    tps: float
    ml: float        # кг/ч, что насчитал блок
    lam: float       # что в цилиндре
    stft: float
    ltft: float
    ect: float
    enrich: bool = False     # обогащение на ускорение
    closed: bool = False     # регулятор по узкому зонду работает
    cut: bool = False        # топливо перекрыто
    adc: float = 0.0         # АЦП ДМРВ, 10 бит: номер точки тарировки × 2
    rl: float = 0.0          # наполнение по блоку, %
    lcmd: float = 1.0        # заданная лямбда (0x9818)
    o2v: float = 0.45        # узкий зонд (УДК), В


def narrowband(lam: float, rnd=None) -> float:
    """
    Штатный узкий зонд: почти ступенька у лямбды 1. Богаче 0.98 -- около
    0.85 В, беднее 1.02 -- около 0.1 В, между ними круто. Сколько именно
    бедно или богато, он не говорит.
    """
    x = (lam - 1.0) / 0.006
    v = 0.1 + 0.75 / (1.0 + math.exp(x))
    if rnd is not None:
        v += rnd.gauss(0, 0.01)
    return min(1.1, max(0.0, v))


def _rl_true(rpm: float, tps: float) -> float:
    rlmax = 82 + 8 * math.sin(min(rpm, 6500) / 6500 * math.pi)
    return rlmax * (1 - math.exp(-tps / 12.0)) + 1.5


def run(fm_ecu, sensor_raw, err=err_lean_top, drive=DRIVE, seed=1,
        ltft=3.0, step=0.02, pk=None, by_code: bool = False,
        stft_max: float = 25.0, nb_ctrl: bool = False) -> list[Truth]:
    """
    Прогнать поездку. fm_ecu -- прошивка блока (FuelModel), sensor_raw --
    тарировка, которой на самом деле соответствует датчик (исходная).
    pk(обороты, дроссель) -- поправка пульсаций, на которую блок множит
    расход до того, как отдать его в OBD.

    by_code -- когда работает регулятор по зонду. По умолчанию -- как
    принято думать: на полной нагрузке выключен. by_code=True -- как
    по коду блока (0x84BB04, docs/41): выключает его не полная нагрузка,
    а цель, отличная от ровно 1.000, и нагрузка выше 99.75 %. У v2M цель
    на полном газе 1.000 -- и регулятор там работает.

    nb_ctrl -- регулятор как у настоящего узкого зонда: «качели». Зонд
    говорит только «богато» или «бедно», и STFT в ответ скачком P при
    каждом переключении и наклоном I между ними ходит пилой вокруг нужного
    значения. Без него -- плавное подтягивание, как по ШДК.
    """
    rnd = random.Random(seed)
    fac, ofs = fm_ecu.hfm_factor, fm_ecu.hfm_ofs
    sens = [r * fac - ofs for r in sensor_raw]
    ecu = [r * fac - ofs for r in fm_ecu.hfm_raw]
    n = len(sens)

    def volt_index(q_tab):
        lo, hi = 0, n - 1
        if q_tab <= sens[0]:
            return 0.0
        if q_tab >= sens[-1]:
            return float(n - 1)
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if sens[mid] < q_tab:
                lo = mid
            else:
                hi = mid
        a, b = sens[lo], sens[hi]
        return lo + ((q_tab - a) / (b - a) if b > a else 0.0)

    def ecu_flow(fi):
        # блок между точками не интерполирует (docs/36), но отсчёты
        # усредняются -- в среднем выходит линейно
        i = int(fi)
        if i >= n - 1:
            return ecu[-1]
        return ecu[i] + (ecu[i + 1] - ecu[i]) * (fi - i)

    out = []
    t, stft, ect = 0.0, 0.0, 60.0
    was_rich = None
    # газ доходит до зонда не сразу: регулятор видит смесь 0.12 с назад.
    # Без этой задержки P-скачок перекидывает зонд на каждом шаге, и
    # «качели» залипают где попало в полосе шириной в скачок
    seen = [1.0] * max(1, int(round(0.12 / step)))
    last_tps, enrich_until = 0.0, -1.0
    for dur, r0, r1, tps in drive:
        steps = int(dur / step)
        for k in range(steps):
            rpm = r0 + (r1 - r0) * k / max(1, steps - 1)
            rpm += rnd.gauss(0, 6)
            ect = min(90.0, ect + step * 0.6)
            if tps - last_tps > 20:
                enrich_until = t + 0.6
            last_tps = tps
            p = pk(rpm, tps) if pk else 1.0
            q_true = _rl_true(rpm, tps) * rpm * fm_ecu.kumsrl
            q_tab = q_true / err(q_true)            # датчик врёт
            fi = volt_index(q_tab)                  # напряжение датчика
            ml = ecu_flow(fi) * p                   # что насчитал блок
            real = q_true * p
            rl = fm_ecu.rl(rpm, ml)
            lcmd, fl = fm_ecu.lam_cmd(rpm, tps, rl)
            cut = tps < 1.0 and rpm > 1400
            if by_code:
                off = cut or ect < 70 or abs(lcmd - 1.0) > 1e-9 or rl >= 99.75
            else:
                off = fl or cut or ect < 70
            if off:
                stft = 0.0
                closed = False
            else:
                closed = True
            fuel = ml / lcmd * (1 + stft / 100) * (1 + ltft / 100)
            if t < enrich_until:
                fuel *= 1.12
            lam = 3.0 if cut else real / fuel
            seen.append(lam)
            lam_seen = seen.pop(0)
            if closed and nb_ctrl:
                rich = narrowband(lam_seen) > 0.45
                if was_rich is not None and rich != was_rich:
                    stft += -2.0 if rich else 2.0          # скачок P
                stft += (-8.0 if rich else 8.0) * step     # наклон I, %/с
                stft = max(-stft_max, min(stft_max, stft))
                was_rich = rich
            elif closed:
                # интегратор: тянет к единице за ~0.4 с
                stft += (lam - 1.0) * 100 * step / 0.4
                stft = max(-stft_max, min(stft_max, stft))
            else:
                was_rich = None
            out.append(Truth(t, rpm, tps, ml, lam, stft, ltft, ect,
                             t < enrich_until, closed, cut,
                             min(1023.0, fi * 2), rl, lcmd,
                             narrowband(lam, rnd)))
            t += step
    return out


def _q(x, s):
    return round(x / s) * s


def ecu_log_carscanner(truth: list[Truth], rate=8.0, seed=2,
                       pids=None) -> str:
    """Лог ELM как у Car Scanner: длинный, по одному параметру на строку."""
    rnd = random.Random(seed)
    pids = PIDS if pids is None else pids
    lines = ['"SECONDS";"PID";"VALUE";"UNITS"']
    i = 0
    period = 1.0 / rate
    t = 0.0
    T = truth[-1].t
    dt = truth[1].t - truth[0].t
    while t < T:
        k = min(int(t / dt), len(truth) - 1)
        s = truth[k]
        name, unit = pids[i % len(pids)]
        v = {"Engine RPM": _q(s.rpm, 0.25),
             "Mass air flow rate": _q(s.ml / 3.6, 0.01),
             "Throttle position": _q(s.tps, 100 / 255),
             "Short term fuel % trim - Bank 1": _q(s.stft, 100 / 128),
             "Long term fuel % trim - Bank 1": _q(s.ltft, 100 / 128),
             "Engine coolant temperature": round(s.ect),
             "O2 sensor 1 voltage": _q(s.o2v, 0.005),
             "Fuel system status": 2.0 if s.closed else 4.0}[name]
        lines.append('"%.3f";"%s";"%s";"%s"' % (t, name, ("%.2f" % v), unit))
        i += 1
        t += period * (0.8 + 0.4 * rnd.random())
    return "\n".join(lines) + "\n"


def wb_log(truth: list[Truth], offset=12.3, lag=0.3, rate=10.0,
           seed=3) -> str:
    """
    Лог ШДК своим логгером: десять раз в секунду, свой ноль часов.

    offset -- на сколько часы ЭБУ впереди: время ЭБУ = время ШДК + offset.
    Десятичная запятая и табуляция -- как у русской локали.
    """
    rnd = random.Random(seed)
    dt = truth[1].t - truth[0].t
    lines = ["Логгер ШДК, экспорт", "", "Время, с\tЛямбда\tAFR"]
    t_ecu = offset           # лог ШДК начат на offset секунд позже
    while t_ecu < truth[-1].t:
        k = int((t_ecu - lag) / dt)
        if k >= 0:
            lam = min(truth[k].lam, 1.9) + rnd.gauss(0, 0.004)
            lines.append(("%.2f\t%.3f\t%.2f" % (t_ecu - offset, lam,
                                                lam * 14.7)).replace(".", ","))
        t_ecu += 1.0 / rate
    return "\n".join(lines) + "\n"
