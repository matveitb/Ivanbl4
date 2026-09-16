#!/usr/bin/env python3
"""
Запуск окна: python3 -m editor [прошивка.bin] [описание.a2l]

Без доводов открывается последняя пара файлов, если она запомнена, иначе
пустое окно с приглашением открыть файлы.
"""

from __future__ import annotations

import os
import sys

from . import paths                                         # noqa: F401

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "ui"))


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    from PySide6 import QtWidgets, QtCore
    from main_window import MainWindow, ORG, APP

    binp = a2l = ""
    for a in argv:
        if a.lower().endswith(".a2l"):
            a2l = a
        elif os.path.exists(a):
            binp = a

    app = QtWidgets.QApplication([sys.argv[0]])
    app.setApplicationName(APP)
    app.setOrganizationName(ORG)

    if not (binp and a2l):
        s = QtCore.QSettings(ORG, APP)
        binp = binp or s.value("last_bin", "")
        a2l = a2l or s.value("last_a2l", "")
        if not (binp and a2l and os.path.exists(binp) and os.path.exists(a2l)):
            binp = a2l = ""

    w = MainWindow(binp, a2l)
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
