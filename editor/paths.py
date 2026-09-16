#!/usr/bin/env python3
"""
Настройка путей импорта.

Проект исторически держит инструменты плоско в tools/ и импортирует их
друг из друга по имени (import fwlib), полагаясь на PYTHONPATH. Редактор
живёт пакетом, но пользоваться теми же модулями должен. Здесь это
однократно улаживается, чтобы каждый модуль редактора не повторял
три строчки с sys.path.
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

for sub in ("tools", os.path.join("editor", "a2l"),
            os.path.join("editor", "core")):
    p = os.path.join(ROOT, sub)
    if p not in sys.path:
        sys.path.insert(0, p)
