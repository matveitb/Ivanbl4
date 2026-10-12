#!/usr/bin/env python3
"""
Чтение логов: ELM-сканеры, логгеры УДК, любой CSV.

Логов два вида, и оба надо читать без вопросов к человеку.

* ДЛИННЫЙ -- одна строка на одно показание: время, имя параметра,
  значение, единицы. Так пишет Car Scanner: ELM опрашивает параметры по
  очереди, и у каждого своё время.
* ШИРОКИЙ -- строка на момент времени, столбец на параметр. Так пишут
  Torque, LogWorks и почти все логгеры УДК. Пустые клетки и прочерки --
  обычное дело: ELM не успевает опросить всё за один проход.

Поэтому внутри лог хранится не таблицей, а КАНАЛАМИ: у каждого параметра
свои моменты времени и свои значения, только те, что действительно
пришли. Сводит каналы на общую шкалу уже udk.py, интерполяцией и с
ограничением на разрыв -- подставлять значение, которого не было, нельзя.

Что учитывается само:

* кодировка: UTF-8 (с BOM и без) или Windows-1251;
* разделитель: точка с запятой, табуляция или запятая;
* десятичная запятая (русская локаль), если разделитель не запятая;
* строки шапки над таблицей (LogWorks пишет туда имя файла и дату);
* время числом секунд, «ЧЧ:ММ:СС.ссс» или с датой -- тогда известен и
  абсолютный момент начала, и два лога можно свести по часам.

Если столбца времени нет вовсе, время берётся по номеру строки с шагом
`dt` -- и это записывается в примечание, а не подразумевается молча.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
from dataclasses import dataclass, field

DELIMS = (";", "\t", ",")
MISSING = {"", "-", "--", "—", "n/a", "na", "nan", "null", "none", "?"}

_TIME_NAMES = re.compile(
    r"^(t|time|время|секунд\w*|seconds?|sec|timestamp|"
    r"device time|gps time|time\s*\(s(ec)?\)|время\s*\(с\)|время,\s*с|"
    r"время,\s*сек|t,\s*с|t\s*\(с\))$", re.I)
_LONG_TIME = re.compile(r"^(seconds?|time|время|t|секунд\w*)$", re.I)
_LONG_NAME = re.compile(r"^(pid|name|parameter|параметр|имя|датчик|sensor)$",
                        re.I)
_LONG_VALUE = re.compile(r"^(value|значение|val)$", re.I)
_LONG_UNIT = re.compile(r"^(units?|единицы|ед\.?|ед\. изм\.?)$", re.I)

_HMS = re.compile(r"(\d{1,2}):(\d{2}):(\d{2}(?:[.,]\d+)?)")
_MONTHS = {m: i + 1 for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"))}
_DATE_YMD = re.compile(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})")
_DATE_DMY = re.compile(r"(\d{1,2})[-./](\d{1,2}|[A-Za-z]{3})[-./](\d{2,4})")


@dataclass
class Channel:
    name: str
    unit: str = ""
    t: list = field(default_factory=list)     # секунды: как в файле или от начала
    v: list = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.t)

    @property
    def label(self) -> str:
        return "%s, %s" % (self.name, self.unit) if self.unit else self.name


@dataclass
class LogFile:
    path: str = ""
    fmt: str = ""                 # "long" | "wide"
    channels: dict = field(default_factory=dict)   # имя -> Channel
    t0: float | None = None       # абсолютный момент начала, если известен
    t0_kind: str = ""             # "epoch" -- с датой, "day" -- только часы
    note: str = ""

    @property
    def duration(self) -> float:
        ends = [c.t[-1] for c in self.channels.values() if c.t]
        return max(ends) if ends else 0.0

    @property
    def name(self) -> str:
        return os.path.basename(self.path) if self.path else "лог"


# -- числа и время ---------------------------------------------------------

def to_float(s: str, comma_decimal: bool) -> float | None:
    s = s.strip().strip('"').strip()
    if s.lower() in MISSING:
        return None
    s = s.replace(" ", "").replace(" ", "")
    if comma_decimal:
        s = s.replace(",", ".")
    try:
        x = float(s)
    except ValueError:
        return None
    if x != x or x in (float("inf"), float("-inf")):
        return None
    return x


def parse_time(s: str, comma_decimal: bool) -> tuple[float | None, str]:
    """
    Время в секундах и его род.

    "rel" -- просто число секунд (от начала или от чего угодно);
    "day" -- часы:минуты:секунды без даты, секунды от полуночи;
    "epoch" -- с датой, секунды от 1970 года.
    """
    x = to_float(s, comma_decimal)
    if x is not None:
        return x, "rel"
    s = s.strip().strip('"')
    m = _HMS.search(s)
    if not m:
        return None, ""
    sec = (int(m.group(1)) * 3600 + int(m.group(2)) * 60
           + float(m.group(3).replace(",", ".")))
    head = s[:m.start()]
    d = None
    my = _DATE_YMD.search(head)
    if my:
        try:
            d = _dt.date(int(my.group(1)), int(my.group(2)), int(my.group(3)))
        except ValueError:
            d = None
    else:
        md = _DATE_DMY.search(head)
        if md:
            mon = md.group(2)
            mon = (_MONTHS.get(mon.lower()[:3]) if mon.isalpha()
                   else int(mon))
            y = int(md.group(3))
            y += 2000 if y < 100 else 0
            try:
                d = _dt.date(y, mon, int(md.group(1))) if mon else None
            except ValueError:
                d = None
    if d is None:
        return sec, "day"
    epoch = _dt.datetime(d.year, d.month, d.day,
                         tzinfo=_dt.timezone.utc).timestamp()
    return epoch + sec, "epoch"


# -- разбор текста ----------------------------------------------------------

def decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def _split(line: str, delim: str) -> list[str]:
    """Разбить строку с учётом кавычек: "1,5";"Engine RPM"."""
    out, cur, q = [], [], False
    for ch in line:
        if ch == '"':
            q = not q
        elif ch == delim and not q:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [x.strip().strip('"').strip() for x in out]


def _sniff(lines: list[str]) -> str:
    """
    Разделитель -- тот, что даёт одинаковое число полей в большинстве
    строк. Запятая проверяется последней: при десятичной запятой она
    встречается в каждом числе, и простой подсчёт выбрал бы её.
    """
    best, score = ",", -1
    sample = lines[:60]
    for d in DELIMS:
        counts = [len(_split(x, d)) for x in sample]
        if not counts:
            continue
        top = max(set(counts), key=counts.count)
        if top < 2:
            continue
        s = counts.count(top) * 100 + top
        if s > score:
            best, score = d, s
    return best


def _is_numberish(cell: str, comma_decimal: bool) -> bool:
    if cell.strip().lower() in MISSING:
        return True
    if to_float(cell, comma_decimal) is not None:
        return True
    return bool(_HMS.search(cell))


def _find_header(rows: list[list[str]], comma_decimal: bool) -> int:
    """
    Строка шапки -- последняя текстовая перед сплошными числами.

    LogWorks и самодельные логгеры пишут над таблицей имя файла, дату,
    версию; их надо пропустить, а не принять за имена столбцов.
    """
    width = max((len(r) for r in rows[:60]), default=0)
    for i, r in enumerate(rows[:60]):
        if len(r) < 2 or len(r) < width * 0.6:
            continue
        nxt = [x for x in rows[i + 1:i + 6] if len(x) >= 2]
        if not nxt:
            continue
        texty = sum(1 for c in r if c and not _is_numberish(c, comma_decimal))
        if texty < max(1, len(r) // 2):
            continue
        numeric_rows = 0
        for x in nxt:
            num = sum(1 for c in x if _is_numberish(c, comma_decimal))
            if num >= max(1, len(x) - 2):
                numeric_rows += 1
        if numeric_rows >= max(1, len(nxt) // 2):
            return i
    return 0


_UNIT_IN_NAME = re.compile(r"^(.*?)[\s,]*[\(\[]([^()\[\]]{1,12})[\)\]]\s*$")


_UNIT_AFTER_COMMA = re.compile(r"^(.*\S)\s*,\s*([^,]{1,8})$")


def _name_unit(head: str) -> tuple[str, str]:
    """«Engine RPM(rpm)» -> ("Engine RPM", "rpm"); «Обороты, об/мин» -- так же."""
    m = _UNIT_IN_NAME.match(head)
    if m and m.group(1).strip():
        return m.group(1).strip(), m.group(2).strip()
    m = _UNIT_AFTER_COMMA.match(head.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return head.strip(), ""


def parse_text(text: str, path: str = "", dt: float = 0.1) -> LogFile:
    lines = [x for x in text.splitlines()
             if x.strip() and not x.lstrip().startswith(("#", "//"))]
    if not lines:
        raise ValueError("лог пуст")
    delim = _sniff(lines)
    comma_decimal = delim != ","
    rows = [_split(x, delim) for x in lines]
    h = _find_header(rows, comma_decimal)
    head = rows[h]
    body = rows[h + 1:]

    low = [c.lower() for c in head]
    it = next((i for i, c in enumerate(low) if _LONG_TIME.match(c)), -1)
    iname = next((i for i, c in enumerate(low) if _LONG_NAME.match(c)), -1)
    ival = next((i for i, c in enumerate(low) if _LONG_VALUE.match(c)), -1)
    if it >= 0 and iname >= 0 and ival >= 0:
        log = _parse_long(body, it, iname, ival,
                          next((i for i, c in enumerate(low)
                                if _LONG_UNIT.match(c)), -1),
                          comma_decimal)
    else:
        log = _parse_wide(head, body, comma_decimal, dt)
    log.path = path
    if not log.channels:
        raise ValueError("в логе не нашлось ни одного числового столбца")
    return log


def read_log(path: str, dt: float = 0.1) -> LogFile:
    with open(path, "rb") as fh:
        raw = fh.read()
    return parse_text(decode(raw), path=path, dt=dt)


def _finish_times(times: list, kinds: set) -> tuple[list, float | None, str]:
    """Привести моменты к секундам от начала, заодно найти абсолютный ноль."""
    if not times:
        return times, None, ""
    kind = "epoch" if "epoch" in kinds else ("day" if "day" in kinds else "")
    out = list(times)
    if kind in ("day", ""):
        # Переход через полночь: время вдруг уменьшилось на полсуток с
        # лишним. Бывает и у просто секунд: Car Scanner последних версий
        # пишет в SECONDS секунды от местной полуночи.
        add = 0.0
        prev = out[0]
        for i, x in enumerate(out):
            if x + add < prev - 43200:
                add += 86400
            out[i] = x + add
            prev = out[i]
    if not kind:
        # просто секунды: оставляем как в файле, чтобы сдвиг, введённый
        # руками, совпадал с тем, что человек видит в логах
        return out, None, ""
    t0 = min(out)
    return [x - t0 for x in out], t0, kind


def _parse_long(body, it, iname, ival, iunit, comma_decimal) -> LogFile:
    log = LogFile(fmt="long")
    raw_t, kinds, recs = [], set(), []
    for r in body:
        if max(it, iname, ival) >= len(r):
            continue
        t, kind = parse_time(r[it], comma_decimal)
        v = to_float(r[ival], comma_decimal)
        if t is None or v is None or not r[iname]:
            continue
        kinds.add(kind)
        unit = r[iunit] if 0 <= iunit < len(r) else ""
        raw_t.append(t)
        recs.append((r[iname], unit, v))
    times, log.t0, log.t0_kind = _finish_times(raw_t, kinds)
    for t, (name, unit, v) in zip(times, recs):
        ch = log.channels.get(name)
        if ch is None:
            ch = log.channels[name] = Channel(name, unit)
        ch.t.append(t)
        ch.v.append(v)
    for ch in log.channels.values():
        _sort(ch)
    return log


def _parse_wide(head, body, comma_decimal, dt) -> LogFile:
    log = LogFile(fmt="wide")
    # столбец времени: по имени, а если не нашёлся -- первый, если в нём
    # время в виде часов
    tcols = [i for i, c in enumerate(head) if _TIME_NAMES.match(c.strip())]
    # у Torque два времени -- GPS и прибора; прибор надёжнее, GPS бывает
    # пустым, пока нет спутников
    pref = [i for i in tcols if "device" in head[i].lower()]
    itime = pref[0] if pref else (tcols[0] if tcols else -1)
    if itime < 0 and body and body[0] and _HMS.search(body[0][0]):
        itime = 0

    raw_t, kinds, keep = [], set(), []
    for n, r in enumerate(body):
        if itime >= 0:
            if itime >= len(r):
                continue
            t, kind = parse_time(r[itime], comma_decimal)
            if t is None:
                continue
            kinds.add(kind)
        else:
            t = n * dt
        raw_t.append(t)
        keep.append(r)
    times, log.t0, log.t0_kind = _finish_times(raw_t, kinds)
    if itime < 0:
        log.note = ("столбца времени нет: время взято по номеру строки, "
                    "шаг %.3g с" % dt)

    skip = set(tcols) | ({itime} if itime >= 0 else set())
    used_names: dict = {}
    for j, h in enumerate(head):
        if j in skip or not h.strip():
            continue
        name, unit = _name_unit(h)
        if name in used_names:              # одинаковые имена бывают
            used_names[name] += 1
            name = "%s #%d" % (name, used_names[name])
        else:
            used_names[name] = 1
        ch = Channel(name, unit)
        for t, r in zip(times, keep):
            if j < len(r):
                v = to_float(r[j], comma_decimal)
                if v is not None:
                    ch.t.append(t)
                    ch.v.append(v)
        if len(ch) >= 2:
            _sort(ch)
            log.channels[name] = ch
    return log


def _sort(ch: Channel) -> None:
    if any(ch.t[i] > ch.t[i + 1] for i in range(len(ch.t) - 1)):
        pairs = sorted(zip(ch.t, ch.v), key=lambda p: p[0])
        ch.t = [p[0] for p in pairs]
        ch.v = [p[1] for p in pairs]


# -- выборка значения в момент времени ---------------------------------------

def sample(ch: Channel, t: float, max_gap: float) -> float | None:
    """
    Значение канала в момент t: линейно между соседними показаниями.

    Если ближайшие показания дальше max_gap -- значения нет. ELM опрашивает
    параметры по кругу, и между двумя показаниями оборотов может пройти
    секунда; интерполировать через пятисекундную дыру -- значит выдумывать.
    """
    ts = ch.t
    n = len(ts)
    if n == 0 or t < ts[0] - max_gap or t > ts[-1] + max_gap:
        return None
    lo, hi = 0, n
    while lo < hi:
        mid = (lo + hi) // 2
        if ts[mid] < t:
            lo = mid + 1
        else:
            hi = mid
    i = lo
    if i < n and ts[i] == t:
        return ch.v[i]
    if i == 0:
        return ch.v[0] if ts[0] - t <= max_gap else None
    if i == n:
        return ch.v[-1] if t - ts[-1] <= max_gap else None
    a, b = ts[i - 1], ts[i]
    if b - a > 2 * max_gap:
        # дыра в логе: годится только очень близкое показание
        if t - a <= max_gap / 2:
            return ch.v[i - 1]
        if b - t <= max_gap / 2:
            return ch.v[i]
        return None
    k = (t - a) / (b - a) if b > a else 0.0
    return ch.v[i - 1] + (ch.v[i] - ch.v[i - 1]) * k


def shifted(ch: Channel, dt: float) -> Channel:
    """Тот же канал, сдвинутый по времени на dt секунд."""
    return Channel(ch.name, ch.unit, [x + dt for x in ch.t], list(ch.v))
