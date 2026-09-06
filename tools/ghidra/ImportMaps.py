# Импорт найденных карт в Ghidra как метки, данные и закладки.
#
# Запуск: Ghidra -> Window -> Script Manager -> ImportMaps.py
# Скрипт спросит путь к JSON, полученному из tools/mapscan.py --out
#
# Создаёт для каждой карты:
#   * метку MAP_xxx по адресу заголовка;
#   * закладку с описанием (размерность, разрядность, оценка);
#   * определённые данные: байты/слова счётчиков, массивы осей, массив тела;
#   * комментарий с осями.
#
#@category Firmware
#@runtime Jython

import json

from ghidra.program.model.data import (ByteDataType, WordDataType,
                                       ArrayDataType, UnsignedIntegerDataType)
from ghidra.program.model.symbol import SourceType


def get_addr(offset):
    return currentProgram.getAddressFactory().getDefaultAddressSpace().getAddress(offset)


def clear_and_define(addr, dt, length):
    try:
        listing = currentProgram.getListing()
        listing.clearCodeUnits(addr, addr.add(max(0, length - 1)), False)
        listing.createData(addr, dt)
        return True
    except Exception as exc:
        print("  не удалось определить данные по %s: %s" % (addr, exc))
        return False


def main():
    f = askFile("Выберите JSON с картами (mapscan --out)", "Открыть")
    with open(f.getAbsolutePath()) as fh:
        payload = json.load(fh)

    maps = payload.get("maps", [])
    print("Карт в файле: %d" % len(maps))

    base = currentProgram.getImageBase().getOffset()
    print("Image base: 0x%X" % base)

    created = 0
    for i, m in enumerate(maps):
        try:
            addr = get_addr(base + m["addr"])
            dims = ("%dx%d" % (m["nx"], m["ny"])) if m["kind"] == "3d" else str(m["nx"])
            name = "MAP_%03d_%06X_%s" % (i, m["addr"], dims)

            createLabel(addr, name, True, SourceType.USER_DEFINED)

            desc = ("%s %s | ось u%d, данные u%d | оценка %.2f | гладкость %.2f"
                    % (m["kind"], dims, m["axis_width"] * 8, m["data_width"] * 8,
                       m.get("score", 0.0), m.get("smooth", 0.0)))
            createBookmark(addr, "Kennfeld", desc)

            aw = m["axis_width"]
            dw = m["data_width"]
            aty = ByteDataType() if aw == 1 else WordDataType()
            dty = ByteDataType() if dw == 1 else WordDataType()

            # счётчики
            ncnt = 2 if m["kind"] == "3d" else 1
            clear_and_define(addr, ArrayDataType(ByteDataType(), ncnt, 1), ncnt)

            # ось X
            xo = addr.add(ncnt)
            clear_and_define(xo, ArrayDataType(aty, m["nx"], aw), m["nx"] * aw)

            # ось Y
            off = ncnt + m["nx"] * aw
            if m["kind"] == "3d":
                yo = addr.add(off)
                clear_and_define(yo, ArrayDataType(aty, m["ny"], aw), m["ny"] * aw)
                off += m["ny"] * aw

            # тело карты
            do = addr.add(off)
            cells = m["nx"] * m["ny"]
            clear_and_define(do, ArrayDataType(dty, cells, dw), cells * dw)

            cmt = "ось X: %s" % (m.get("x_axis") or [])
            if m.get("y_axis"):
                cmt += "\nось Y: %s" % m["y_axis"]
            cmt += "\nтело карты: 0x%X (%d ячеек по %d байт)" % (m["data_addr"], cells, dw)
            setPlateComment(addr, cmt)

            created += 1
        except Exception as exc:
            print("Ошибка на карте #%d (0x%X): %s" % (i, m.get("addr", -1), exc))

    print("Готово. Размечено карт: %d из %d" % (created, len(maps)))


main()
