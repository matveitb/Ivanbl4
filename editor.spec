# -*- mode: python ; coding: utf-8 -*-
"""
Сборка редактора в один исполняемый файл.

Главная сложность -- ПЛОСКИЕ ИМПОРТЫ. Проект исторически держит
инструменты в tools/ и импортирует их по имени (import fwlib), а модули
редактора лежат по подкаталогам и тоже видят друг друга по имени
(import geometry). PyInstaller строит граф импортов статически и таких
модулей не находит: в собранном файле их просто не окажется, и программа
упадёт на первом же открытии прошивки.

Поэтому два списка ниже -- не перестраховка, а необходимость:
  pathex        -- где искать эти модули при анализе;
  hiddenimports -- какие именно взять, раз по коду их не видно.

Описание Spectra кладём внутрь: без A2L редактор бесполезен, а искать его
на чужой машине неоткуда. Чужие описания открываются как обычно.

    pyinstaller editor.spec
"""

import os

ROOT = os.path.abspath(os.getcwd())

# Имя файла ЛАТИНИЦЕЙ намеренно, хотя всё остальное в программе по-русски.
# Собирает его сборочная машина Windows через CI, и кириллица в имени
# артефакта -- это лишний повод для кракозябр там, где я не могу ни
# посмотреть, ни поправить. Переименовать скачанный файл можно как угодно:
# он один и ни от чего рядом не зависит.
APP_NAME = "CalibrationEditor"

PATHS = [
    os.path.join(ROOT, "tools"),
    os.path.join(ROOT, "editor", "a2l"),
    os.path.join(ROOT, "editor", "core"),
    os.path.join(ROOT, "editor", "ui"),
]

HIDDEN = [
    # разбор ASAP2
    "lexer", "parser", "model", "geometry",
    # ядро
    "mapaccess", "compare", "edits", "saving", "project",
    # окно
    "main_window", "tree", "grid", "plot2d", "plot3d", "palette", "diffview",
    # инструменты проекта, которые ядро зовёт по имени
    "fwlib", "bosch_csum", "fwdiff", "torque", "a2l_legacy",
]

DATAS = [
    (os.path.join(ROOT, "results", "FBH3ID60_legacy.a2l"), "results"),
]

a = Analysis(
    ["editor_main.py"],
    pathex=PATHS,
    binaries=[],
    datas=DATAS,
    hiddenimports=HIDDEN,
    hookspath=[],
    runtime_hooks=[],
    # Лишнее из PySide6: графики рисуем сами на QPainter, сеть и веб не
    # нужны вовсе. Снимает с готового файла заметный вес.
    excludes=[
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
        "PySide6.QtQuick", "PySide6.QtQml", "PySide6.Qt3DCore",
        "PySide6.QtMultimedia", "PySide6.QtCharts",
        "PySide6.QtDataVisualization", "PySide6.QtNetwork",
        "tkinter", "matplotlib", "numpy", "unittest", "pydoc",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)



def build(name, console):
    return EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name=name,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        runtime_tmpdir=None,
        console=console,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )


# То, что получает человек: окно без консоли.
exe = build(APP_NAME, False)

# То же самое, но с консолью -- для самопроверки на сборочной машине.
# У оконной программы под Windows stdout писать некуда, и вывод
# самопроверки пропал бы вместе с ответом на вопрос, работает ли сборка.
# Содержимое обеих одинаковое: анализ и архив общие.
exe_check = build("selftest", True)
