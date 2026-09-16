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
    # Русские названия карт лежат ОТДЕЛЬНЫМ файлом (сам A2L ASCII-only), и
    # забыть его -- значит собрать .exe, где всё подписано по-бошевски.
    (os.path.join(ROOT, "results", "FBH3ID60_legacy.ru.json"), "results"),
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



COMMON = dict(
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX не трогаем ВООБЩЕ. Сжатый исполняемый файл -- главный признак,
    # по которому эвристики антивирусов записывают программу в упаковщики,
    # а выигрыш в размере того не стоит.
    upx=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)


def onefile(name, console):
    return EXE(pyz, a.scripts, a.binaries, a.datas, [],
               name=name, runtime_tmpdir=None, console=console, **COMMON)


# --- ОСНОВНАЯ сборка: папкой ------------------------------------------
#
# Одиночный .exe устроен как самораспаковывающийся архив: на старте он
# разворачивает Python и полсотни библиотек во временный каталог и оттуда
# запускает. Это ровно тот почерк, по которому эвристики антивирусов ловят
# упаковщики и дропперы, и неподписанный одиночный файл они заворачивают
# регулярно -- вплоть до того, что браузер не даёт его скачать.
#
# Сборка папкой так не выглядит: рядом с exe лежат обычные DLL, ничего
# никуда не распаковывается. Ложных срабатываний заметно меньше.
# Раздаётся zip-архивом, и это ещё и обходит запрет браузера на скачивание
# неподписанных исполняемых файлов.
exe_dir = EXE(pyz, a.scripts, [], exclude_binaries=True,
              name=APP_NAME, console=False, **COMMON)
coll = COLLECT(exe_dir, a.binaries, a.datas, strip=False, upx=False,
               name=APP_NAME)

# --- Запасная сборка: одним файлом ------------------------------------
# Кому удобнее один файл и у кого антивирус не возражает.
exe = onefile(APP_NAME + "-onefile", False)

# --- Самопроверка ------------------------------------------------------
# С консолью: у оконной программы под Windows stdout писать некуда, и
# вывод самопроверки пропал бы вместе с ответом на вопрос, работает ли
# сборка. Анализ и архив общие со всеми остальными, значит проверяется
# ровно то же содержимое.
exe_check = onefile("selftest", True)
