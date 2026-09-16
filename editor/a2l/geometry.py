#!/usr/bin/env python3
"""
Раскладка карты: где именно в файле лежат её данные и оси.

Это самый ответственный слой. Ошибка здесь не видна глазом -- таблица
просто покажет соседние байты, и правка испортит другую карту.

Три вещи, которые надо решить правильно.

1. АДРЕС В CHARACTERISTIC ОЗНАЧАЕТ РАЗНОЕ. Если раскладка содержит только
   FNC_VALUES, адрес указывает прямо на данные. Если в ней есть
   AXIS_PTS_X или NO_AXIS_PTS_X, оси лежат внутри блока, и адрес
   указывает на ЗАГОЛОВОК -- данные начинаются за счётчиками и осями.
   Различаем по содержимому раскладки, а не по её имени: имена у разных
   генераторов свои.

2. ПОРЯДОК ЭЛЕМЕНТОВ БЛОКА ЗАДАН ПОЗИЦИЯМИ, А НЕ ИМЕНАМИ. У карт Bosch
   с осями внутри блока первой в файле лежит ось СТРОК, и раскладка
   объявляет её первой позицией. Разбирать "сначала X, раз он X" значит
   прочитать карту транспонированной: на квадратной незаметно, на 16 на
   12 -- испорченная правка.

3. ЧИСЛО ТОЧЕК В A2L -- ЭТО МАКСИМУМ, А НЕ ФАКТ. Поле в AXIS_DESCR
   называется MaxAxisPoints. Когда оси лежат внутри блока, настоящее
   число точек записано в самом файле (NO_AXIS_PTS_X/Y), и брать надо
   его. Иначе на карте с запасом по размеру поедет всё.

4. АДРЕСА БЫВАЮТ АБСОЛЮТНЫЕ И ФАЙЛОВЫЕ. У нас в A2L лежат файловые
   смещения (0x10529), а стандартный генератор пишет абсолютные
   (0x810529). Разницу берёт на себя AddressMap, и определяется она не
   догадкой, а подсчётом: какой вариант укладывает больше карт внутрь
   файла.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import model
from model import A2lFile, Characteristic, CompuMethod, DTYPE, RecordLayout


@dataclass
class AddressMap:
    """Перевод адреса из A2L в смещение в файле."""
    subtract: int = 0
    size: int = 0

    def off(self, addr: int) -> int:
        return addr - self.subtract

    def ok(self, off: int, length: int = 1) -> bool:
        return 0 <= off and off + length <= self.size


def detect_addressing(a2l: A2lFile, size: int,
                      candidates=(0, 0x800000, 0x80000, 0x8000000)) -> AddressMap:
    """
    Подобрать вычитаемую базу по тому, сколько карт попадает внутрь файла.

    Считаем, а не угадываем: для каждой базы смотрим долю карт, чьи
    адреса после вычитания оказались в пределах образа. Побеждает та, у
    которой доля выше; при равенстве -- меньшая база (то есть «как есть»).
    """
    addrs = [c.addr for c in a2l.characteristics.values() if c.addr]
    addrs += [a.addr for a in a2l.axis_pts.values() if a.addr]
    if not addrs:
        return AddressMap(0, size)
    best, best_hits = 0, -1
    for base in candidates:
        hits = sum(1 for a in addrs if 0 <= a - base < size)
        if hits > best_hits:
            best, best_hits = base, hits
    return AddressMap(best, size)


@dataclass
class AxisSource:
    """Откуда берутся точки оси."""
    kind: str = "index"        # inline | ref | fixed | index
    off: int = 0               # для inline и ref
    count: int = 0
    width: int = 1
    signed: bool = False
    factor: float = 1.0
    offset: float = 0.0
    unit: str = ""
    start: float = 0.0         # для fixed
    step: float = 1.0

    def values(self, buf: bytes) -> list[float]:
        if self.kind == "fixed":
            return [self.start + i * self.step for i in range(self.count)]
        if self.kind == "index":
            return [float(i) for i in range(self.count)]
        out = []
        for i in range(self.count):
            a = self.off + i * self.width
            raw = int.from_bytes(buf[a:a + self.width], "little",
                                 signed=self.signed)
            out.append(raw * self.factor + self.offset)
        return out


@dataclass
class MapLayout:
    name: str
    ctype: str
    data_off: int
    nx: int                    # столбцов
    ny: int                    # строк
    width: int
    signed: bool
    row_dir: bool
    factor: float
    offset: float
    unit: str
    editable: bool = True
    note: str = ""
    header_off: int = -1       # если оси лежат внутри блока
    x_axis: AxisSource = field(default_factory=AxisSource)
    y_axis: AxisSource = field(default_factory=AxisSource)

    @property
    def cells(self) -> int:
        return self.nx * self.ny

    @property
    def size(self) -> int:
        return self.cells * self.width

    @property
    def end(self) -> int:
        return self.data_off + self.size

    def raw_range(self) -> tuple[int, int]:
        bits = 8 * self.width
        if self.signed:
            return -(1 << (bits - 1)), (1 << (bits - 1)) - 1
        return 0, (1 << bits) - 1


def _axis_from_descr(a2l: A2lFile, am: AddressMap, descr, buf: bytes,
                     count: int) -> AxisSource:
    """Ось по ссылке, с постоянным шагом или просто индекс."""
    if descr is None:
        return AxisSource("index", count=count)
    if descr.kind == "FIX_AXIS" or (descr.fix_step and not descr.axis_pts_ref):
        return AxisSource("fixed", count=count,
                          start=descr.fix_start, step=descr.fix_step)
    if descr.axis_pts_ref:
        ap = a2l.axis_pts.get(descr.axis_pts_ref)
        if ap:
            rl = a2l.layout_of(ap.record_layout)
            # у оси данные лежат в FNC_VALUES её собственной раскладки,
            # а перед ними может стоять счётчик
            w, sg = DTYPE.get(rl.axis_x_dtype or rl.fnc_dtype, (1, False))
            cw = rl.count_width("x")
            cm = a2l.compu_of(ap.compu)
            off = am.off(ap.addr) + cw
            n = count
            if cw:
                got = int.from_bytes(buf[am.off(ap.addr):am.off(ap.addr) + cw],
                                     "little")
                if 0 < got <= count:
                    n = got
            return AxisSource("ref", off=off, count=n, width=w, signed=sg,
                              factor=cm.factor, offset=cm.offset, unit=cm.unit)
    return AxisSource("index", count=count)


def resolve(a2l: A2lFile, name: str, buf: bytes,
            am: AddressMap | None = None) -> MapLayout:
    """Собрать раскладку карты по её имени."""
    ch = a2l.characteristics.get(name)
    if ch is None:
        raise KeyError("в A2L нет карты %s" % name)
    if am is None:
        am = detect_addressing(a2l, len(buf))
    return resolve_ch(a2l, ch, buf, am)


def resolve_ch(a2l: A2lFile, ch: Characteristic, buf: bytes,
               am: AddressMap) -> MapLayout:
    rl = a2l.layout_of(ch.record_layout)
    cm = a2l.compu_of(ch.compu)
    width, signed = rl.width()
    base = am.off(ch.addr)

    ax = ch.axes[0] if len(ch.axes) > 0 else None
    ay = ch.axes[1] if len(ch.axes) > 1 else None

    # --- размерность -----------------------------------------------------
    if ch.ctype == "VALUE":
        nx, ny = 1, 1
    elif ch.ctype in ("VAL_BLK", "ASCII"):
        nx = ch.number or (ch.matrix_dim[0] if ch.matrix_dim else 1)
        ny = ch.matrix_dim[1] if len(ch.matrix_dim) > 1 else 1
    else:
        nx = ax.max_points if ax else 1
        ny = ay.max_points if ay else 1
        if ch.ctype == "CURVE":
            ny = 1

    header_off = -1
    note = ""

    # --- где данные ------------------------------------------------------
    if rl.has_inline_axes and ch.ctype in ("CURVE", "MAP"):
        header_off = base
        awx = rl.axis_width("x")
        awy = rl.axis_width("y") if ay else 0
        # Идём по элементам блока В ПОРЯДКЕ ИХ ПОЗИЦИЙ. Не в порядке
        # "сначала X, потом Y": у карт Bosch первой в файле лежит ось
        # строк, и раскладка объявляет её первой позицией. Порядок разбора
        # должен идти из раскладки, а не из названий осей.
        pos = base
        x_src = y_src = None
        for key, which in rl.order():
            if which == "nx":
                w = rl.count_width("x")
                got = int.from_bytes(buf[pos:pos + w], "little")
                # настоящее число точек записано в файле, а в A2L -- максимум
                if 0 < got <= max(nx, 256):
                    nx = got
                pos += w
            elif which == "ny":
                if not ay:
                    continue
                w = rl.count_width("y")
                got = int.from_bytes(buf[pos:pos + w], "little")
                if 0 < got <= max(ny, 256):
                    ny = got
                pos += w
            elif which == "ax":
                x_src = AxisSource("inline", off=pos, count=nx, width=awx,
                                   signed=False, factor=1.0, offset=0.0)
                pos += nx * awx
            elif which == "ay":
                if not ay:
                    continue
                y_src = AxisSource("inline", off=pos, count=ny, width=awy,
                                   signed=False, factor=1.0, offset=0.0)
                pos += ny * awy
        if x_src is None:
            x_src = AxisSource("index", count=nx)
        if y_src is None:
            y_src = AxisSource("index", count=1 if not ay else ny)
        # масштаб осей всё равно берём из их AXIS_DESCR
        if ax:
            acm = a2l.compu_of(ax.compu)
            x_src.factor, x_src.offset, x_src.unit = (acm.factor, acm.offset,
                                                      acm.unit)
        if ay:
            acm = a2l.compu_of(ay.compu)
            y_src.factor, y_src.offset, y_src.unit = (acm.factor, acm.offset,
                                                      acm.unit)
        data_off = pos
        note = "оси внутри блока, заголовок 0x%05X" % header_off
    else:
        data_off = base
        x_src = _axis_from_descr(a2l, am, ax, buf, nx)
        y_src = _axis_from_descr(a2l, am, ay, buf, ny) if ay \
            else AxisSource("index", count=1)

    lay = MapLayout(name=ch.name, ctype=ch.ctype, data_off=data_off,
                    nx=max(1, nx), ny=max(1, ny), width=width, signed=signed,
                    row_dir=rl.row_dir, factor=cm.factor, offset=cm.offset,
                    unit=cm.unit, editable=cm.linear, note=note,
                    header_off=header_off, x_axis=x_src, y_axis=y_src)

    if not cm.linear:
        lay.note = (lay.note + "; " if lay.note else "") + \
            "нелинейный пересчёт %s -- правка запрещена" % cm.kind
    if not am.ok(lay.data_off, lay.size):
        lay.editable = False
        lay.note = (lay.note + "; " if lay.note else "") + \
            "выходит за пределы образа"
    return lay


def resolve_all(a2l: A2lFile, buf: bytes,
                am: AddressMap | None = None) -> dict:
    if am is None:
        am = detect_addressing(a2l, len(buf))
    out = {}
    for name, ch in a2l.characteristics.items():
        try:
            out[name] = resolve_ch(a2l, ch, buf, am)
        except Exception as exc:                            # noqa: BLE001
            out[name] = MapLayout(name=name, ctype=ch.ctype, data_off=0,
                                  nx=1, ny=1, width=1, signed=False,
                                  row_dir=True, factor=1.0, offset=0.0,
                                  unit="", editable=False,
                                  note="не разобрана: %s" % exc)
    return out
