#!/usr/bin/env python3
"""
Настройка путей импорта.

Проект исторически держит инструменты плоско в tools/ и импортирует их
друг из друга по имени (import fwlib), полагаясь на PYTHONPATH. Редактор
живёт пакетом, но пользоваться теми же модулями должен. Здесь это
однократно улаживается, чтобы каждый модуль редактора не повторял
три строчки с sys.path.

Про собранный .exe. Внутри сборки PyInstaller никаких каталогов tools/ и
editor/core рядом нет: все модули лежат в общей таблице замороженных и
доступны по имени сразу. Поэтому под заморозкой не подкладываем ничего --
но и не падаем, а просто ничего не делаем. Проверять надо ИМЕННО
sys.frozen, а не наличие каталога: каталог может случайно оказаться рядом
с exe и увести импорт на чужой код.
"""

from __future__ import annotations

import os
import sys

FROZEN = getattr(sys, "frozen", False)
ROOT = (getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)) if FROZEN
        else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if not FROZEN:
    for sub in ("tools", os.path.join("editor", "a2l"),
                os.path.join("editor", "core"), os.path.join("editor", "ui")):
        p = os.path.join(ROOT, sub)
        if p not in sys.path:
            sys.path.insert(0, p)


def bundled(*parts: str) -> str:
    """
    Путь к файлу, положенному внутрь сборки (описание A2L и прочее).

    В обычном запуске это просто путь от корня проекта.
    """
    return os.path.join(ROOT, *parts)
