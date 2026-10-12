#!/usr/bin/env python3
"""
Командная оболочка редактора -- то же ядро, но без окна.

Нужна затем, что ядро должно быть проверяемо без Qt: разбор A2L, адресная
арифметика, пересчёт значений и сравнение прошивок не зависят от
интерфейса и не должны требовать его для запуска.

    python3 -m editor.cli list --a2l F.a2l --bin FW.bin
    python3 -m editor.cli show --a2l F.a2l --bin FW.bin --map KFZWOP
    python3 -m editor.cli diff --a2l F.a2l --bin A.bin --other B.bin
    python3 -m editor.cli udk  --a2l F.a2l --bin FW.bin --log elm.csv \
        [--wb udk.csv] [--target hfm|kllamfa] [--out новая.bin]
    python3 -m editor.cli log  --elm COM3 [--wb COM4 --wb-proto isp2] \
        [--out лог.csv] [--seconds 600]
"""

from __future__ import annotations

import argparse
import sys

from . import paths                                         # noqa: F401
import geometry                                             # noqa: E402
import model                                                # noqa: E402
import mapaccess as M                                       # noqa: E402


def _load(a2l_path: str, bin_path: str):
    a2l = model.load(a2l_path)
    buf = open(bin_path, "rb").read()
    am = geometry.detect_addressing(a2l, len(buf))
    return a2l, buf, am


def cmd_list(a) -> int:
    a2l, buf, am = _load(a.a2l, a.bin)
    lays = geometry.resolve_all(a2l, buf, am)
    rows = sorted(lays.values(), key=lambda L: (L.ctype, L.name))
    if a.grep:
        import re
        rx = re.compile(a.grep, re.I)
        rows = [L for L in rows if rx.search(L.name)]
    print("%-24s %-6s %-9s %-9s %-7s %s"
          % ("ИМЯ", "ТИП", "ДАННЫЕ", "РАЗМЕР", "ШИРИНА", "ЕДИНИЦЫ"))
    for L in rows[:a.limit]:
        print("%-24s %-6s 0x%05X   %3dx%-5d %-7s %s%s"
              % (L.name, L.ctype, L.data_off, L.nx, L.ny,
                 ("s" if L.signed else "u") + str(L.width * 8),
                 L.unit, "" if L.editable else "  [только чтение]"))
    print("\nвсего %d, показано %d" % (len(lays), min(len(rows), a.limit)))
    print("адресация: вычитаем 0x%X" % am.subtract)
    return 0


def cmd_show(a) -> int:
    a2l, buf, am = _load(a.a2l, a.bin)
    L = geometry.resolve(a2l, a.map, buf, am)
    print("%s  %s  данные 0x%05X  %dx%d  %s%d  множитель %g  %s"
          % (L.name, L.ctype, L.data_off, L.nx, L.ny,
             "s" if L.signed else "u", L.width * 8, L.factor, L.unit))
    if L.note:
        print("примечание: " + L.note)
    print()
    print(M.fmt_table(buf, L, width=a.width, digits=a.digits))
    return 0


def cmd_diff(a) -> int:
    from compare import compare_files
    a2l, buf, am = _load(a.a2l, a.bin)
    other = open(a.other, "rb").read()
    res = compare_files(a2l, buf, other, am)
    if not res.maps:
        print("карты не отличаются")
    print("%-24s %-9s %-22s %s"
          % ("КАРТА", "ЯЧЕЕК", "ДЕЛЬТА", "ЕДИНИЦЫ"))
    for d in res.maps:
        print("%-24s %3d/%-5d %9.3f .. %-9.3f %s"
              % (d.name, d.changed, d.total, d.dmin, d.dmax, d.unit))
    print("\nизменённых карт: %d, байт всего: %d, вне карт: %d байт"
          % (len(res.maps), res.changed_bytes, res.unmapped_bytes))
    return 0


def cmd_udk(a) -> int:
    """Смесь по УДК: отчёт и, если задан --out, исправленная прошивка."""
    import logio
    import project as proj
    import udk

    p = proj.Project.open(a.a2l, a.bin)
    st = udk.Settings(target=a.target, lam_want=a.want, lag=a.lag,
                      variant=a.variant, bts=a.bts, lam_kind=a.lam,
                      maf_unit=a.maf, max_rich=a.max_rich,
                      max_lean=a.max_lean, ect_min=a.ect_min)
    fm = udk.FuelModel.from_project(p, a.variant)
    ecu = logio.read_log(a.log)
    wb = logio.read_log(a.wb) if a.wb else None
    roles = udk.detect_roles(ecu, wb)
    for spec in a.role or []:
        role, _, name = spec.partition("=")
        src = "wb" if wb is not None and name in wb.channels else "ecu"
        roles[role] = (src, name) if name else None
    print("Каналы:")
    for role, title, _ in udk.ROLES:
        r = roles.get(role)
        print("  %-26s %s" % (title, ("%s: %s" % r) if r else "--"))
    if wb is not None:
        if a.offset == "auto":
            off, how = udk.find_offset(ecu, wb, roles, st)
            if off is None:
                print("Сдвиг логов не найден: " + how, file=sys.stderr)
                return 2
            print("Сдвиг лога УДК: %+.2f с (%s)" % (off, how))
            st.offset = off
        else:
            st.offset = float(a.offset)
    print()
    res = udk.analyze(fm, ecu, wb, roles, st)
    print(udk.report(res))
    if a.out:
        eds = udk.apply(p, res)
        if not eds:
            print("\nПрошивка не изменилась -- записывать нечего")
            return 0
        r = p.save(a.out)
        print("\nЗаписано %s: %s; сумм исправлено %d из %d%s" % (
            a.out, ", ".join("%s %d яч." % (e.map_name, e.count) for e in eds),
            r.fixed, r.records, ("; " + r.note) if r.note else ""))
        print("После прошивки сбросьте адаптацию (снять клемму) и снимите "
              "новый лог: поправка считается за несколько проходов.")
    return 0


def cmd_log(a) -> int:
    """Снять лог: ELM и УДК в один файл. Ctrl+C -- стоп."""
    import time
    import elm as E
    import livelog
    import obd
    import wideband

    fm = None
    if a.a2l and a.bin:
        import project as proj
        import udk
        fm = udk.FuelModel.from_project(proj.Project.open(a.a2l, a.bin))
    car = None
    if a.elm == "demo" or a.wb == "demo":
        import fakecar
        if fm is None:
            print("для имитатора нужны --a2l и --bin", file=sys.stderr)
            return 2
        car = fakecar.FakeCar(fm)

    if a.elm == "demo":
        t = fakecar.FakeElm(car, time.monotonic, realtime=True)
    elif a.elm.startswith("wifi"):
        _, _, addr = a.elm.partition(":")
        host, _, port = (addr or "192.168.0.10:35000").partition(":")
        t = E.TcpTransport(host, int(port or 35000))
    else:
        t = E.connect_serial(a.elm, a.baud, print)
    e = E.Elm327(t)
    e.init(a.proto, print)
    sup = e.supported_pids()
    print("блок отдаёт PID: " + " ".join("%02X" % p for p in sorted(sup)))

    wt = parser = None
    if a.wb:
        kw = {"kind": a.wb_kind} if a.wb_proto == "ascii" else {}
        parser = wideband.make(a.wb_proto, **kw)
        if a.wb == "demo":
            wt = fakecar.FakeWideband(car, time.monotonic, proto=a.wb_proto,
                                      realtime=True)
        else:
            wt = E.SerialTransport(a.wb, a.wb_baud or parser.baud)
    out = a.out or time.strftime("лог_%Y-%m-%d_%H-%M-%S.csv")
    L = livelog.LiveLogger(e, wt, parser, path=out, supported=sup)
    L.start()
    print("пишу в %s, Ctrl+C -- стоп" % out)
    names = {"об/мин": obd.PIDS[0x0C].name, "г/с": obd.PIDS[0x10].name,
             "дроссель": obd.PIDS[0x11].name, "λ": livelog.LAMBDA_NAME}
    try:
        end = time.monotonic() + a.seconds if a.seconds else None
        while end is None or time.monotonic() < end:
            time.sleep(1.0)
            last = L.latest()
            bits = ["%s %s" % (k, ("%.3f" % last[n][1]) if k == "λ"
                               else ("%.0f" % last[n][1]))
                    for k, n in names.items() if n in last]
            print("%6.0f с  %s%s" % (L.clock(), "  ".join(bits),
                                     ("  ! " + L.error) if L.error else ""))
            L.error = ""
    except KeyboardInterrupt:
        pass
    L.stop()
    e.close()
    if wt is not None:
        wt.close()
    print("записано: %s" % out)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Редактор калибровок без окна")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--a2l", required=True)
        p.add_argument("--bin", required=True)

    p = sub.add_parser("list", help="перечислить карты")
    common(p)
    p.add_argument("--grep")
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("show", help="показать карту таблицей")
    common(p)
    p.add_argument("--map", required=True)
    p.add_argument("--width", type=int, default=8)
    p.add_argument("--digits", type=int, default=2)
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("diff", help="сравнить с другой прошивкой")
    common(p)
    p.add_argument("--other", required=True)
    p.set_defaults(fn=cmd_diff)

    p = sub.add_parser("udk", help="смесь по УДК: поправки по логу")
    common(p)
    p.add_argument("--log", required=True, help="лог ЭБУ (ELM): CSV")
    p.add_argument("--wb", help="отдельный лог УДК, если он своим файлом")
    p.add_argument("--offset", default="auto",
                   help="сдвиг лога УДК, с: время ЭБУ = время УДК + сдвиг; "
                        "auto -- подобрать по возвратам топлива")
    p.add_argument("--target", choices=["hfm", "kllamfa"], default="hfm",
                   help="hfm -- тарировка ДМРВ, kllamfa -- лямбда полного газа")
    p.add_argument("--want", type=float, default=None,
                   help="желаемая лямбда на полном газе (для kllamfa); "
                        "без него -- та, что уже в прошивке")
    p.add_argument("--lag", type=float, default=0.3,
                   help="задержка УДК относительно ЭБУ, с")
    p.add_argument("--variant", type=int, choices=[0, 1], default=0)
    p.add_argument("--bts", action="store_true",
                   help="считать, что защита компонентов KFLBTS работала")
    p.add_argument("--lam", choices=["auto", "lambda", "afr"], default="auto")
    p.add_argument("--maf", choices=["auto", "g/s", "kg/h"], default="auto")
    p.add_argument("--max-rich", type=float, default=25.0)
    p.add_argument("--max-lean", type=float, default=10.0)
    p.add_argument("--ect-min", type=float, default=70.0)
    p.add_argument("--role", action="append",
                   help="задать канал руками: роль=имя (rpm, maf, tps, lam, "
                        "stft, ltft, ect)")
    p.add_argument("--out", help="записать исправленную прошивку сюда")
    p.set_defaults(fn=cmd_udk)

    p = sub.add_parser("log", help="снять лог: ELM327 и УДК в один файл")
    p.add_argument("--elm", required=True,
                   help="COM3, /dev/ttyUSB0, wifi[:host:port] или demo")
    p.add_argument("--baud", type=int, default=0,
                   help="скорость COM-порта ELM; 0 -- подобрать")
    p.add_argument("--proto", default="0", choices=["0", "3", "4", "5"],
                   help="ATSP: 0 -- искать, 5 -- KWP быстрый, 4 -- KWP 5 бод, "
                        "3 -- ISO 9141")
    p.add_argument("--wb", help="порт УДК (COM4) или demo; без него -- "
                                "только блок")
    p.add_argument("--wb-proto", default="isp2",
                   choices=sorted(__import__("wideband").PARSERS))
    p.add_argument("--wb-baud", type=int, default=0)
    p.add_argument("--wb-kind", default="auto",
                   choices=["auto", "lambda", "afr"])
    p.add_argument("--out", help="файл лога")
    p.add_argument("--seconds", type=float, default=0,
                   help="остановиться через столько секунд")
    p.add_argument("--a2l", help="для имитатора: описание")
    p.add_argument("--bin", help="для имитатора: прошивка")
    p.set_defaults(fn=cmd_log)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
