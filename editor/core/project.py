#!/usr/bin/env python3
"""
Проект: образ, описание, правки и группировка в одном месте.

Окно не должно знать ни про адресную арифметику, ни про историю правок,
ни про то, откуда взялось дерево. Всё это здесь, и то же самое доступно
из командной строки -- иначе проверять пришлось бы мышью.

Про дерево. Группировок две, и переключаются они явно, а не сами:

* ИЗ ОПИСАНИЯ -- блоки GROUP из A2L (стандарт ASAP2; наш генератор их
  пишет, чужие файлы приносят свои). Если групп нет, раскладываем ПО
  ПЕРВЫМ БУКВАМ ИМЕНИ: Bosch именует по приставкам (KF... -- карта,
  KL... -- кривая), и даже такое дерево лучше плоского списка на
  восемьсот строк.
* СВОЯ -- то, что человек собрал руками; живёт в файле проекта (*.ktp).

Раньше своя группировка просто перебивала описание, стоило завести одну
группу. Это неверно: собрав папку "над чем работаю", человек терял
разбивку по контурам целиком. Теперь режим переключается в меню "Вид", и
своя группировка ничего не прячет, пока её не выбрали.

Заголовки групп в A2L записаны транслитом: файл делается ASCII-only ради
старых версий WinOLS. Здесь они переводятся обратно по таблице
генератора -- но только если она нашлась, и только для совпавших имён.
Чужой A2L покажет свои заголовки как есть.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import geometry                                             # noqa: E402
import model                                                # noqa: E402
import saving                                               # noqa: E402
from edits import History                                   # noqa: E402

PREFIXES = {
    "KF": "Карты KF", "KL": "Кривые KL", "KT": "Таблицы KT",
    "CW": "Кодовые слова CW", "SG": "Диагностика SG",
    "MAP": "Безымянные находки", "AXIS": "Оси", "SRL": "Оси", "SNM": "Оси",
}


def _titles() -> dict:
    """Русские заголовки групп из генератора -- если он рядом."""
    try:
        from a2l_legacy import SECTION_TITLES
    except Exception:                                       # noqa: BLE001
        return {}
    out = {}
    try:
        from a2l_legacy import ident, translit
    except Exception:                                       # noqa: BLE001
        return {}
    for key, ru in SECTION_TITLES.items():
        out[ident("G_" + translit(key), set())] = ru
    return out


@dataclass
class TreeNode:
    title: str
    maps: list = field(default_factory=list)
    children: list = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.maps) + sum(c.count for c in self.children)


@dataclass
class Project:
    a2l_path: str = ""
    bin_path: str = ""
    a2l: object = None
    buf: bytearray = field(default_factory=bytearray)
    original: bytes = b""
    am: object = None
    layouts: dict = field(default_factory=dict)
    history: History = field(default_factory=History)
    user_groups: dict = field(default_factory=dict)     # заголовок -> [имена]
    fix_checksums: bool = True
    csum_table: int = -1
    group_mode: str = "auto"        # auto -- из описания, user -- своя
    _overlaps: dict = None          # считается лениво, при первом спросе

    # -- открытие --------------------------------------------------------

    @classmethod
    def open(cls, a2l_path: str, bin_path: str) -> "Project":
        a2l = model.load(a2l_path)
        data = open(bin_path, "rb").read()
        am = geometry.detect_addressing(a2l, len(data))
        p = cls(a2l_path=a2l_path, bin_path=bin_path, a2l=a2l,
                buf=bytearray(data), original=data, am=am)
        p.layouts = geometry.resolve_all(a2l, data, am)
        p.csum_table = saving.find_table(data)
        p.load_project_file()
        return p

    def reload_bin(self, path: str) -> None:
        """Сменить образ, оставив описание: частый случай при переносе правок."""
        data = open(path, "rb").read()
        self.bin_path = path
        self.buf = bytearray(data)
        self.original = data
        self.am = geometry.detect_addressing(self.a2l, len(data))
        self.layouts = geometry.resolve_all(self.a2l, data, self.am)
        self.csum_table = saving.find_table(data)
        self.history = History()
        self._overlaps = None

    # -- дерево ----------------------------------------------------------

    def tree(self) -> TreeNode:
        root = TreeNode("Карты")
        titles = _titles()
        known = set(self.layouts)

        if self.group_mode == "user":
            for title, names in self.user_groups.items():
                root.children.append(
                    TreeNode(title, [n for n in names if n in known]))
            placed = {n for c in root.children for n in c.maps}
            rest = sorted(known - placed)
            if rest:
                root.children.append(TreeNode("Вне своих групп", rest))
            return root

        groups = [g for g in self.a2l.groups.values() if not g.root]
        if groups:
            for g in sorted(groups, key=lambda g: -len(g.characteristics)):
                kept = [n for n in g.characteristics if n in known]
                if kept:
                    root.children.append(
                        TreeNode(titles.get(g.name) or g.desc or g.name, kept))
            placed = {n for c in root.children for n in c.maps}
            rest = sorted(known - placed)
            if rest:
                root.children.append(TreeNode("Вне групп", rest))
            return root

        # запасной порядок: по приставке имени
        buckets: dict = {}
        for name in sorted(known):
            key = "Прочее"
            for pref, title in PREFIXES.items():
                if name.upper().startswith(pref):
                    key = title
                    break
            buckets.setdefault(key, []).append(name)
        for title in sorted(buckets, key=lambda k: -len(buckets[k])):
            root.children.append(TreeNode(title, buckets[title]))
        return root

    def _build_overlaps(self) -> None:
        """
        Кто с кем делит байты.

        Описание не обязано быть непротиворечивым: адреса приходят из
        разных источников, и две карты вполне могут претендовать на одни и
        те же байты. Для описи это неточность, для редактора -- прямая
        порча: правишь одну карту, молча меняется соседняя. Запрещать
        нечего, человек может знать, что делает, но молчать нельзя.

        Считаем один раз при открытии, заметанием по отсортированным
        началам, а не сравнением всех со всеми.
        """
        self._overlaps = {}
        items = sorted((L for L in self.layouts.values() if L.size),
                       key=lambda L: L.data_off)
        for i, a in enumerate(items):
            for b in items[i + 1:]:
                if b.data_off >= a.end:
                    break
                self._overlaps.setdefault(a.name, []).append(b.name)
                self._overlaps.setdefault(b.name, []).append(a.name)

    def overlaps(self, name: str) -> list:
        if self._overlaps is None:
            self._build_overlaps()
        return self._overlaps.get(name, [])

    def search(self, text: str) -> list:
        """Поиск по имени и по описанию. Пустой запрос -- все карты."""
        t = (text or "").strip().lower()
        if not t:
            return sorted(self.layouts)
        out = []
        for name in sorted(self.layouts):
            ch = self.a2l.characteristics.get(name)
            hay = name.lower() + " " + (ch.desc.lower() if ch else "")
            if t in hay:
                out.append(name)
        return out

    # -- свои группы -----------------------------------------------------

    def add_to_group(self, title: str, names) -> None:
        """
        Положить карты в свою группу. Карта может лежать только в одной:
        иначе «сколько всего карт» перестаёт сходиться, а перекладывание
        из группы в группу превращается в поиск, где ещё она осталась.
        """
        names = [n for n in names if n in self.layouts]
        for other, lst in self.user_groups.items():
            if other != title:
                self.user_groups[other] = [n for n in lst if n not in names]
        cur = self.user_groups.setdefault(title, [])
        cur.extend(n for n in names if n not in cur)

    def remove_from_group(self, title: str, names) -> None:
        if title in self.user_groups:
            names = set(names)
            self.user_groups[title] = [n for n in self.user_groups[title]
                                       if n not in names]

    def drop_group(self, title: str) -> None:
        self.user_groups.pop(title, None)

    def rename_group(self, old: str, new: str) -> None:
        if old in self.user_groups and new and new != old:
            self.user_groups = {(new if k == old else k): v
                                for k, v in self.user_groups.items()}

    def desc(self, name: str) -> str:
        ch = self.a2l.characteristics.get(name)
        return ch.desc if ch else ""

    # -- правки ----------------------------------------------------------

    def layout(self, name: str):
        return self.layouts[name]

    def apply(self, edit):
        return self.history.apply(self.buf, self.layouts[edit.map_name], edit)

    def undo(self):
        return self.history.undo(self.buf, self.layouts)

    def redo(self):
        return self.history.redo(self.buf, self.layouts)

    @property
    def dirty(self) -> bool:
        return self.history.dirty

    def changed_maps(self) -> set:
        """Какие карты отличаются от исходного образа."""
        out = set()
        for name, L in self.layouts.items():
            if L.size and bytes(self.buf[L.data_off:L.end]) != \
                    self.original[L.data_off:L.end]:
                out.add(name)
        return out

    # -- сохранение ------------------------------------------------------

    def save(self, path: str = "") -> saving.SaveResult:
        path = path or self.bin_path
        res = saving.save(self.buf, path, fix_checksums=self.fix_checksums,
                          table=self.csum_table)
        self.bin_path = path
        self.history.mark_saved()
        self.save_project_file()
        return res

    # -- файл проекта ----------------------------------------------------

    @property
    def project_path(self) -> str:
        return os.path.splitext(self.bin_path)[0] + ".ktp"

    def load_project_file(self) -> bool:
        p = self.project_path
        if not os.path.exists(p):
            return False
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:                                   # noqa: BLE001
            return False
        self.user_groups = {k: list(v) for k, v in
                            (d.get("groups") or {}).items()}
        if "fix_checksums" in d:
            self.fix_checksums = bool(d["fix_checksums"])
        if d.get("group_mode") in ("auto", "user"):
            self.group_mode = d["group_mode"]
        if d.get("a2l") and not os.path.exists(self.a2l_path):
            self.a2l_path = d["a2l"]
        return True

    def save_project_file(self) -> None:
        d = {"a2l": self.a2l_path, "bin": os.path.basename(self.bin_path),
             "groups": self.user_groups, "fix_checksums": self.fix_checksums,
             "group_mode": self.group_mode}
        with open(self.project_path, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=2)
