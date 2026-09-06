# Импорт разметки карт в Ghidra.
#
# Вход: out/FBH3ID60_maps.json (результат tools/report.py --json)
# Поддерживается и старый формат из tools/mapscan.py --out.
#
# Загружайте образ как Raw Binary с базой 0x800000 -- тогда адреса совпадут
# с адресами из таблицы контрольных сумм. Скрипт прибавляет базу образа сам.
#
#@category Firmware
#@runtime Jython

import json

from ghidra.program.model.data import (ByteDataType, WordDataType, ArrayDataType)
from ghidra.program.model.symbol import SourceType


def get_addr(offset):
    space = currentProgram.getAddressFactory().getDefaultAddressSpace()
    return space.getAddress(offset)


def define(addr, dt, length):
    try:
        listing = currentProgram.getListing()
        listing.clearCodeUnits(addr, addr.add(max(0, length - 1)), False)
        listing.createData(addr, dt)
        return True
    except Exception as exc:
        print("  не удалось определить данные по %s: %s" % (addr, exc))
        return False


def load_maps(path):
    with open(path) as fh:
        payload = json.load(fh)
    maps = payload.get("maps", [])
    out = []
    for m in maps:
        layout = m.get("layout")
        if layout is None:
            # старый формат mapscan: всегда карта с заголовком
            layout = "bosch_header"
        out.append({
            "addr": m["addr"],
            "data_addr": m.get("data_addr", m["addr"]),
            "layout": layout,
            "kind": m.get("kind", "3d"),
            "nx": m["nx"],
            "ny": m.get("ny", 1),
            "axis_width": m.get("axis_width", 1),
            "data_width": m.get("data_width", 1),
            "name": m.get("name"),
            "note": m.get("note", ""),
            "source": m.get("source", ""),
            "confidence": m.get("confidence", ""),
            "x_axis": m.get("x_axis") or [],
            "y_axis": m.get("y_axis") or [],
        })
    return out


def main():
    f = askFile("Выберите JSON с разметкой (report.py --json)", "Открыть")
    maps = load_maps(f.getAbsolutePath())
    print("Карт в файле: %d" % len(maps))

    base = currentProgram.getImageBase().getOffset()
    print("Image base: 0x%X" % base)
    if base == 0:
        print("ВНИМАНИЕ: база образа 0. Для этой прошивки ожидается 0x800000 --")
        print("          метки встанут, но адреса не совпадут с таблицей сумм.")

    created = 0
    for i, m in enumerate(maps):
        try:
            addr = get_addr(base + m["addr"])
            dims = ("%dx%d" % (m["nx"], m["ny"])) if m["kind"] == "3d" else str(m["nx"])
            name = m["name"] or ("MAP_%03d_%05X_%s" % (i, m["addr"], dims))
            name = "".join(c if (c.isalnum() or c == "_") else "_" for c in name)

            createLabel(addr, name, True, SourceType.USER_DEFINED)

            desc = "%s %s | %s" % (m["kind"], dims,
                                   "голая сетка" if m["layout"] == "bare_grid"
                                   else "заголовок Bosch")
            if m["confidence"]:
                desc += " | " + m["confidence"]
            createBookmark(addr, "Kennfeld", desc)

            aw, dw = m["axis_width"], m["data_width"]
            aty = ByteDataType() if aw == 1 else WordDataType()
            dty = ByteDataType() if dw == 1 else WordDataType()
            cells = m["nx"] * m["ny"]

            if m["layout"] == "bosch_header":
                ncnt = 2 if m["kind"] == "3d" else 1
                define(addr, ArrayDataType(ByteDataType(), ncnt, 1), ncnt)
                off = ncnt
                define(addr.add(off), ArrayDataType(aty, m["nx"], aw), m["nx"] * aw)
                off += m["nx"] * aw
                if m["kind"] == "3d":
                    define(addr.add(off), ArrayDataType(aty, m["ny"], aw), m["ny"] * aw)
                    off += m["ny"] * aw
                do = addr.add(off)
            else:
                # тело лежит прямо по адресу, осей в блоке нет
                do = get_addr(base + m["data_addr"])

            define(do, ArrayDataType(dty, cells, dw), cells * dw)

            cmt = "%s  %s" % (name, dims)
            if m["x_axis"]:
                cmt += "\nось X: %s" % m["x_axis"]
            if m["y_axis"]:
                cmt += "\nось Y: %s" % m["y_axis"]
            if m["layout"] == "bare_grid":
                cmt += "\nоси общие, в блоке не хранятся (см. цепочки 0x1008C, 0x181C1)"
            cmt += "\nтело: 0x%X, %d ячеек по %d байт" % (m["data_addr"], cells, dw)
            if m["note"]:
                cmt += "\n" + m["note"]
            setPlateComment(addr, cmt)

            created += 1
        except Exception as exc:
            print("Ошибка на карте #%d (0x%X): %s" % (i, m.get("addr", -1), exc))

    print("Готово. Размечено карт: %d из %d" % (created, len(maps)))


main()
