#!/usr/bin/env python3
"""
Типизированная модель ASAP2 поверх дерева блоков.

Здесь блоки превращаются в объекты, у которых поля разобраны по смыслу.
Всё, чего мы не понимаем, молча остаётся в дереве и сюда не попадает --
так чужой файл с незнакомыми блоками всё равно откроется.

Про пересчёт значений. COMPU_METHOD RAT_FUNC задаёт коэффициенты
преобразования ФИЗИЧЕСКОГО значения в СЫРОЕ:

    raw = (a*p^2 + b*p + c) / (d*p^2 + e*p + f)

Нам нужно обратное. При a = d = e = 0 (а так записаны все линейные
пересчёты) обращение даёт:

    phys = (f * raw - c) / b

то есть множитель f/b и смещение -c/b. Если a или d не нули, пересчёт
нелинейный: такие величины мы ПОКАЗЫВАЕМ, но правку запрещаем -- честнее
отказаться, чем записать не то.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from parser import Block, parse_file

# тип данных ASAP2 -> (ширина в байтах, знаковый)
DTYPE = {
    "UBYTE": (1, False), "SBYTE": (1, True),
    "UWORD": (2, False), "SWORD": (2, True),
    "ULONG": (4, False), "SLONG": (4, True),
    "A_UINT64": (8, False), "A_INT64": (8, True),
    "FLOAT32_IEEE": (4, True), "FLOAT64_IEEE": (8, True),
}


def _f(s: str, default: float = 0.0) -> float:
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def _i(s: str, default: int = 0) -> int:
    try:
        return int(str(s), 0)
    except (TypeError, ValueError):
        return default


@dataclass
class CompuMethod:
    name: str
    desc: str = ""
    kind: str = "IDENTICAL"
    fmt: str = ""
    unit: str = ""
    coeffs: list[float] = field(default_factory=list)
    linear: bool = True
    factor: float = 1.0
    offset: float = 0.0

    @classmethod
    def from_block(cls, b: Block) -> "CompuMethod":
        a = b.args
        cm = cls(name=a[0] if a else "?")
        cm.desc = a[1] if len(a) > 1 else ""
        cm.kind = a[2] if len(a) > 2 else "IDENTICAL"
        cm.fmt = a[3] if len(a) > 3 else ""
        cm.unit = a[4] if len(a) > 4 else ""
        co = b.kv("COEFFS", 6)
        if co:
            cm.coeffs = [_f(x) for x in co]
            ca, cb, cc, cd, ce, cfv = cm.coeffs
            # линейным считаем только случай, который можно обратить точно
            if ca == 0 and cd == 0 and ce == 0 and cb != 0:
                cm.factor = cfv / cb
                cm.offset = -cc / cb
                cm.linear = True
            else:
                cm.linear = False
        elif cm.kind in ("TAB_INTP", "TAB_NOINTP", "TAB_VERB", "FORM"):
            cm.linear = False
        return cm

    def to_phys(self, raw: float) -> float:
        return raw * self.factor + self.offset

    def to_raw(self, phys: float) -> float:
        return (phys - self.offset) / self.factor if self.factor else phys


IDENTITY = CompuMethod("NO_COMPU_METHOD")


@dataclass
class RecordLayout:
    name: str
    fnc_dtype: str = "UBYTE"
    fnc_pos: int = 1
    row_dir: bool = True
    axis_x_dtype: str = ""
    axis_y_dtype: str = ""
    no_axis_x_dtype: str = ""
    no_axis_y_dtype: str = ""

    @classmethod
    def from_block(cls, b: Block) -> "RecordLayout":
        rl = cls(name=b.args[0] if b.args else "?")
        v = b.kv("FNC_VALUES", 3)
        if v:
            rl.fnc_pos = _i(v[0], 1)
            rl.fnc_dtype = v[1]
            rl.row_dir = v[2] != "COLUMN_DIR"
        for key, attr in (("AXIS_PTS_X", "axis_x_dtype"),
                          ("AXIS_PTS_Y", "axis_y_dtype"),
                          ("NO_AXIS_PTS_X", "no_axis_x_dtype"),
                          ("NO_AXIS_PTS_Y", "no_axis_y_dtype")):
            p = b.kv(key, 2)
            if p:
                setattr(rl, attr, p[1])
        return rl

    @property
    def has_inline_axes(self) -> bool:
        """
        Лежат ли оси внутри самого блока карты.

        Это и есть признак, по которому адрес в CHARACTERISTIC означает
        разное: при осях внутри он указывает на ЗАГОЛОВОК, без них -- сразу
        на ДАННЫЕ. Различаем по содержимому раскладки, а не по её имени.
        """
        return bool(self.axis_x_dtype or self.no_axis_x_dtype)

    def width(self) -> tuple[int, bool]:
        return DTYPE.get(self.fnc_dtype, (1, False))

    def axis_width(self, which: str = "x") -> int:
        d = self.axis_x_dtype if which == "x" else self.axis_y_dtype
        return DTYPE.get(d, (1, False))[0]

    def count_width(self, which: str = "x") -> int:
        d = self.no_axis_x_dtype if which == "x" else self.no_axis_y_dtype
        return DTYPE.get(d, (1, False))[0] if d else 0


@dataclass
class AxisPts:
    name: str
    desc: str = ""
    addr: int = 0
    record_layout: str = ""
    compu: str = ""
    max_points: int = 0
    lo: float = 0.0
    hi: float = 0.0

    @classmethod
    def from_block(cls, b: Block) -> "AxisPts":
        """
        Порядок полей AXIS_PTS по стандарту:

            Name, LongIdentifier, Address, InputQuantity, Deposit,
            MaxDiff, Conversion, MaxAxisPoints, LowerLimit, UpperLimit

        Между раскладкой и пересчётом стоит MaxDiff -- поле, которое легко
        не заметить, потому что почти везде равно нулю. Пропустив его,
        разбор берёт пересчётом число 0, молча получает единичный
        множитель и показывает сырые значения оси вместо физических.
        """
        a = b.args
        g = lambda i, d="": a[i] if len(a) > i else d      # noqa: E731
        return cls(name=g(0), desc=g(1), addr=_i(g(2)),
                   record_layout=g(4), compu=g(6),
                   max_points=_i(g(7)), lo=_f(g(8)), hi=_f(g(9)))


@dataclass
class AxisDescr:
    kind: str = "STD_AXIS"
    compu: str = ""
    max_points: int = 0
    lo: float = 0.0
    hi: float = 0.0
    axis_pts_ref: str = ""
    fix_start: float = 0.0
    fix_step: float = 1.0

    @classmethod
    def from_block(cls, b: Block) -> "AxisDescr":
        a = b.args
        g = lambda i, d="": a[i] if len(a) > i else d      # noqa: E731
        ad = cls(kind=g(0, "STD_AXIS"), compu=g(2),
                 max_points=_i(g(3)), lo=_f(g(4)), hi=_f(g(5)))
        ref = b.kv("AXIS_PTS_REF", 1)
        if ref:
            ad.axis_pts_ref = ref[0]
        par = b.kv("FIX_AXIS_PAR_DIST", 3)
        if par:
            ad.fix_start, ad.fix_step = _f(par[0]), _f(par[1], 1.0)
            if not ad.max_points:
                ad.max_points = _i(par[2])
        return ad


@dataclass
class Characteristic:
    name: str
    desc: str = ""
    ctype: str = "VALUE"          # VALUE, CURVE, MAP, VAL_BLK, ASCII
    addr: int = 0
    record_layout: str = ""
    compu: str = ""
    lo: float = 0.0
    hi: float = 0.0
    axes: list[AxisDescr] = field(default_factory=list)
    matrix_dim: list[int] = field(default_factory=list)
    number: int = 0

    @classmethod
    def from_block(cls, b: Block) -> "Characteristic":
        a = b.args
        g = lambda i, d="": a[i] if len(a) > i else d      # noqa: E731
        ch = cls(name=g(0), desc=g(1), ctype=g(2, "VALUE"), addr=_i(g(3)),
                 record_layout=g(4), compu=g(6), lo=_f(g(7)), hi=_f(g(8)))
        ch.axes = [AxisDescr.from_block(x) for x in b.all("AXIS_DESCR")]
        md = b.kv("MATRIX_DIM", 3)
        if md:
            ch.matrix_dim = [_i(x) for x in md if _i(x)]
        num = b.kv("NUMBER", 1)
        if num:
            ch.number = _i(num[0])
        return ch


@dataclass
class Group:
    name: str
    desc: str = ""
    root: bool = False
    characteristics: list[str] = field(default_factory=list)
    subgroups: list[str] = field(default_factory=list)

    @classmethod
    def from_block(cls, b: Block) -> "Group":
        g = cls(name=b.args[0] if b.args else "?",
                desc=b.args[1] if len(b.args) > 1 else "",
                root=b.has("ROOT"))
        ref = b.find("REF_CHARACTERISTIC")
        if ref:
            g.characteristics = list(ref.args)
        sub = b.find("SUB_GROUP")
        if sub:
            g.subgroups = list(sub.args)
        return g


@dataclass
class A2lFile:
    path: str = ""
    project: str = ""
    module: str = ""
    characteristics: dict = field(default_factory=dict)
    axis_pts: dict = field(default_factory=dict)
    compu: dict = field(default_factory=dict)
    layouts: dict = field(default_factory=dict)
    groups: dict = field(default_factory=dict)
    functions: dict = field(default_factory=dict)

    def compu_of(self, name: str) -> CompuMethod:
        return self.compu.get(name, IDENTITY)

    def layout_of(self, name: str) -> RecordLayout:
        return self.layouts.get(name) or RecordLayout(name or "?")


def load(path: str) -> A2lFile:
    root = parse_file(path)
    out = A2lFile(path=path)

    proj = None
    for b in root.children:
        if b.kind == "PROJECT":
            proj = b
            break
    if proj is None:
        raise ValueError("в файле нет блока PROJECT -- это точно A2L?")
    out.project = proj.args[0] if proj.args else ""

    mods = proj.all("MODULE")
    if not mods:
        raise ValueError("в PROJECT нет MODULE")
    mod = mods[0]
    out.module = mod.args[0] if mod.args else ""

    for b in mod.children:
        if b.kind == "CHARACTERISTIC":
            c = Characteristic.from_block(b)
            out.characteristics[c.name] = c
        elif b.kind == "AXIS_PTS":
            a = AxisPts.from_block(b)
            out.axis_pts[a.name] = a
        elif b.kind == "COMPU_METHOD":
            m = CompuMethod.from_block(b)
            out.compu[m.name] = m
        elif b.kind == "RECORD_LAYOUT":
            r = RecordLayout.from_block(b)
            out.layouts[r.name] = r
        elif b.kind == "GROUP":
            g = Group.from_block(b)
            out.groups[g.name] = g
        elif b.kind == "FUNCTION":
            g = Group.from_block(b)
            out.functions[g.name] = g
    return out
