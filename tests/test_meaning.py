#!/usr/bin/env python3
"""
Проверка смыслом: ни одного противоречия -- и проверка умеет их находить.

Первое без второго ничего не стоит. "Ноль противоречий" выдаст и проверка,
которая ничего не сравнивает. Поэтому здесь же имена дамоса нарочно
сдвигаются на одну позицию -- ровно та ошибка, которую даёт выравнивание, --
и проверка обязана её увидеть: противоречия должны появиться, а число карт
с доказанным местом -- обвалиться.
"""

import contextlib
import io
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import meaning  # noqa: E402

A2L = os.path.join(ROOT, "results", "FBH3ID60_legacy.a2l")
FW = os.path.join(ROOT, "firmware", "FBH3ID60_stok.bin")
XFER = os.path.join(ROOT, "results", "damos_перенос.json")

fails = []


def check(cond, msg):
    print(("OK   " if cond else "ОШИБКА ") + msg)
    if not cond:
        fails.append(msg)


def run(xfer):
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "m.json")
        with contextlib.redirect_stdout(io.StringIO()):
            meaning.main(["--a2l", A2L, "--fw", FW, "--xfer", xfer, "--out", out])
        return json.load(open(out, encoding="utf-8"))["maps"]


def tally(maps):
    c = {}
    for v in maps.values():
        c[v["verdict"]] = c.get(v["verdict"], 0) + 1
    return c


def main():
    if not os.path.exists(XFER):
        print("нет %s -- пропуск" % XFER)
        return 0

    real = tally(run(XFER))
    placed = (real.get("смысл и место сходятся", 0)
              + real.get("место доказано рядом", 0)
              + real.get("разобрано вручную", 0)
              + real.get("подтверждено CTP7", 0))
    check(real.get("противоречит", 0) == 0,
          "противоречий между именем и кодом: %d" % real.get("противоречит", 0))
    check(placed > 430, "карт с доказанным местом или смыслом: %d" % placed)

    # -- а теперь имена, сдвинутые на одну позицию
    x = json.load(open(XFER, encoding="utf-8"))
    vs = sorted(x["variables"], key=lambda v: v.get("addr") or 0)
    cv = [(v.get("conv"), v.get("x_conv"), v.get("y_conv")) for v in vs]
    for i, v in enumerate(vs):
        v["conv"], v["x_conv"], v["y_conv"] = cv[(i + 1) % len(vs)]
    x["variables"] = vs
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "shift.json")
        json.dump(x, open(p, "w", encoding="utf-8"), ensure_ascii=False)
        bad = tally(run(p))
    bad_placed = (bad.get("смысл и место сходятся", 0)
                  + bad.get("место доказано рядом", 0))
    check(bad.get("противоречит", 0) >= 10,
          "при сдвинутых именах проверка находит противоречия: %d"
          % bad.get("противоречит", 0))
    check(bad_placed * 10 < placed,
          "при сдвинутых именах место доказано лишь у %d карт против %d"
          % (bad_placed, placed))

    print()
    if fails:
        print("ПРОВАЛЕНО: %d" % len(fails))
        return 1
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
