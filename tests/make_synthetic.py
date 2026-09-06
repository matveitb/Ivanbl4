#!/usr/bin/env python3
"""
Генератор синтетической прошивки для проверки инструментов.

Создаёт образ 512 КБ, состоящий из:
  * псевдослучайного "кода" (высокая энтропия);
  * набора карт в формате Bosch (nx,ny,X,Y,D) с известными адресами;
  * второй копии образа, где изменены только данные "карт зажигания"
    (имитация пары csok-прошивок) плюс пара байт "контрольной суммы".

Ожидаемые адреса пишутся в JSON -- на нём проверяется точность mapscan.
"""

from __future__ import annotations

import json
import os
import random
import struct
import sys

SIZE = 512 * 1024
SEED = 20250906


def smooth_surface(nx: int, ny: int, lo: int, hi: int, tilt: float = 1.0) -> list[int]:
    """Гладкая двумерная поверхность (как настоящая калибровочная карта)."""
    out = []
    for y in range(ny):
        for x in range(nx):
            fx = x / max(1, nx - 1)
            fy = y / max(1, ny - 1)
            v = lo + (hi - lo) * (0.55 * fx + 0.45 * fy * tilt)
            # лёгкая кривизна, как в реальных картах
            v += (hi - lo) * 0.12 * (fx * (1 - fx) * 4) * (1 - fy)
            out.append(int(round(max(lo, min(hi, v)))))
    return out


def axis(n: int, lo: int, hi: int) -> list[int]:
    """Строго возрастающая ось с неравномерным шагом."""
    vals = []
    for i in range(n):
        f = i / max(1, n - 1)
        # сгущение в начале, как у осей нагрузки
        v = lo + (hi - lo) * (f ** 1.35)
        vals.append(int(round(v)))
    # гарантируем строгую монотонность
    for i in range(1, n):
        if vals[i] <= vals[i - 1]:
            vals[i] = vals[i - 1] + 1
    return vals


def pack_map(nx, ny, xs, ys, data, aw, dw) -> bytes:
    fmt_a = "<%d%s" % (nx, "B" if aw == 1 else "H")
    fmt_b = "<%d%s" % (ny, "B" if aw == 1 else "H")
    fmt_d = "<%d%s" % (nx * ny, "B" if dw == 1 else "H")
    return (bytes([nx, ny])
            + struct.pack(fmt_a, *xs)
            + struct.pack(fmt_b, *ys)
            + struct.pack(fmt_d, *data))


def pack_curve(n, xs, data, aw, dw) -> bytes:
    fmt_a = "<%d%s" % (n, "B" if aw == 1 else "H")
    fmt_d = "<%d%s" % (n, "B" if dw == 1 else "H")
    return bytes([n]) + struct.pack(fmt_a, *xs) + struct.pack(fmt_d, *data)


def build(outdir: str) -> None:
    rng = random.Random(SEED)
    buf = bytearray(rng.getrandbits(8) for _ in range(SIZE))

    planted = []
    cursor = 0x18000  # калибровочная область, как в реальных M7.9.7

    def place(blob: bytes, name: str, meta: dict) -> int:
        nonlocal cursor
        addr = cursor
        buf[addr:addr + len(blob)] = blob
        meta = dict(meta, addr=addr, name=name, size=len(blob))
        planted.append(meta)
        cursor += len(blob) + rng.randint(8, 40)  # промежутки между картами
        return addr

    # --- 3D карты ------------------------------------------------------
    specs = [
        # (name, nx, ny, aw, dw, lo, hi, is_ignition)
        ("IGN_MAIN",      16, 16, 1, 1,  20, 210, True),
        ("IGN_PART",      16, 16, 1, 1,  25, 200, True),
        ("IGN_IDLE",       8,  8, 1, 1,  40, 150, True),
        ("FUEL_BASE",     16, 16, 1, 2, 300, 900, False),
        ("VE_MAP",        12, 10, 1, 1,  30, 240, False),
        ("LAMBDA_TARGET",  8, 10, 1, 1,  60, 190, False),
        ("BOOST_LIKE",    10,  8, 2, 2, 500, 4000, False),
    ]
    for name, nx, ny, aw, dw, lo, hi, ign in specs:
        xs = axis(nx, 8, 250 if aw == 1 else 6000)
        ys = axis(ny, 5, 240 if aw == 1 else 5500)
        data = smooth_surface(nx, ny, lo, hi)
        blob = pack_map(nx, ny, xs, ys, data, aw, dw)
        place(blob, name, {"kind": "3d", "nx": nx, "ny": ny,
                           "axis_width": aw, "data_width": dw,
                           "ignition": ign,
                           "data_off": 2 + nx * aw + ny * aw})

    # --- 2D кривые -----------------------------------------------------
    curves = [
        ("WARMUP_ENRICH", 12, 1, 1,  40, 200),
        ("IAT_CORR",      10, 1, 1,  70, 180),
        ("RPM_LIMIT",      8, 1, 2, 900, 6800),
    ]
    for name, n, aw, dw, lo, hi in curves:
        xs = axis(n, 10, 240 if aw == 1 else 6000)
        data = smooth_surface(n, 1, lo, hi)
        blob = pack_curve(n, xs, data, aw, dw)
        place(blob, name, {"kind": "2d", "nx": n, "ny": 1,
                           "axis_width": aw, "data_width": dw,
                           "ignition": False,
                           "data_off": 1 + n * aw})

    stock = bytes(buf)

    # --- вариант "csok_2": меняем ТОЛЬКО данные карт зажигания ---------
    buf2 = bytearray(stock)
    for m in planted:
        if not m.get("ignition"):
            continue
        do = m["addr"] + m["data_off"]
        cells = m["nx"] * m["ny"]
        dw = m["data_width"]
        for i in range(cells):
            off = do + i * dw
            if dw == 1:
                buf2[off] = max(0, min(255, buf2[off] + rng.choice([-6, -4, -3, 3, 4, 6])))
            else:
                v = struct.unpack_from("<H", buf2, off)[0]
                v = max(0, min(0xFFFF, v + rng.choice([-40, -25, 25, 40])))
                struct.pack_into("<H", buf2, off, v)

    # имитация правки контрольной суммы: 4 изолированных байта
    csum_addr = 0x7FFF0
    struct.pack_into("<I", buf2, csum_addr, rng.getrandbits(32))

    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "synth_stock.bin"), "wb") as fh:
        fh.write(stock)
    with open(os.path.join(outdir, "synth_csok2.bin"), "wb") as fh:
        fh.write(bytes(buf2))
    with open(os.path.join(outdir, "synth_expected.json"), "w", encoding="utf-8") as fh:
        json.dump({"maps": planted, "checksum_addr": csum_addr}, fh, indent=1)

    print("Создано в %s: synth_stock.bin, synth_csok2.bin, synth_expected.json" % outdir)
    print("Заложено карт: %d (из них зажигание: %d)" %
          (len(planted), sum(1 for m in planted if m.get("ignition"))))


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "out/synth")
