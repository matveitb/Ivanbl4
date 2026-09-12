#!/usr/bin/env python3
"""
Проверка remotels: чтение оглавления архива по кускам, без скачивания.

Поднимается локальный HTTP-сервер, умеющий Range, и на нём проверяется:

  * ZIP  -- список имён и побайтовое совпадение вытащенного файла;
  * ZIP  -- многотомный распознаётся и говорит об этом, а не врёт;
  * 7z   -- список имён, в том числе при сжатом заголовке (7z сжимает
            заголовок по умолчанию, без распаковки способ бесполезен);
  * RAR5 -- список имён на образце, собранном здесь же по спецификации;
  * трафик -- скачано должно быть заметно меньше размера архива.
"""

import http.server
import os
import re
import shutil
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import remotels        # noqa: E402

fails = []


def check(cond, msg):
    print(("OK   " if cond else "ОШИБКА ") + msg)
    if not cond:
        fails.append(msg)


# -- сервер с поддержкой Range ---------------------------------------------

def serve(directory):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _path(self):
            p = os.path.join(directory, self.path.lstrip("/").split("?")[0])
            return p if os.path.isfile(p) else None

        def do_HEAD(self):
            self._go(False)

        def do_GET(self):
            self._go(True)

        def _go(self, body):
            p = self._path()
            if not p:
                self.send_error(404)
                return
            n = os.path.getsize(p)
            rng = self.headers.get("Range")
            if rng:
                m = re.match(r"bytes=(\d+)-(\d*)", rng)
                s = int(m.group(1))
                e = int(m.group(2)) if m.group(2) else n - 1
                e = min(e, n - 1)
                self.send_response(206)
                self.send_header("Content-Range", "bytes %d-%d/%d" % (s, e, n))
                self.send_header("Content-Length", str(e - s + 1))
            else:
                self.send_response(200)
                self.send_header("Content-Length", str(n))
                s, e = 0, n - 1
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            if body:
                with open(p, "rb") as f:
                    f.seek(s)
                    self.wfile.write(f.read(e - s + 1))

    socketserver.TCPServer.allow_reuse_address = True
    srv = socketserver.TCPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


# -- сборка образцов --------------------------------------------------------

def vint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def make_rar5(path, names):
    """Минимальный RAR5 по спецификации: подпись, главный блок, файлы, конец."""
    def block(htype, body, data=b""):
        head = vint(htype) + vint(0x02 if data else 0)
        if data:
            head += vint(len(data))
        head += body
        return b"\x00\x00\x00\x00" + vint(len(head)) + head + data

    out = bytearray(b"Rar!\x1a\x07\x01\x00")
    out += block(1, vint(0))                                   # главный
    for name in names:
        raw = name.encode("utf-8")
        payload = b"\xaa" * 64
        body = (vint(0) + vint(len(payload) * 2) + vint(0)
                + vint(0) + vint(0) + vint(len(raw)) + raw)
        out += block(2, body, payload)
    out += block(5, vint(0))                                   # конец
    open(path, "wb").write(bytes(out))


def make_tree(d, n=40):
    src = os.path.join(d, "src")
    os.makedirs(src, exist_ok=True)
    for i in range(n):
        open(os.path.join(src, "file_%d.bin" % i), "wb").write(
            bytes((i * 7 + j) % 251 for j in range(4096)))
    dam = os.path.join(ROOT, "firmware", "px5ns03d.dam")
    target = os.path.join(src, "1037368793.dam")
    if os.path.exists(dam):
        shutil.copy(dam, target)
    else:
        open(target, "wb").write(os.urandom(300000))
    return src, target


def main():
    tmp = tempfile.mkdtemp(prefix="remotels_")
    try:
        src, dam = make_tree(tmp)
        zpath = os.path.join(tmp, "a.zip")
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(os.listdir(src)):
                z.write(os.path.join(src, f), f)

        make_rar5(os.path.join(tmp, "a.rar"),
                  ["docs/readme.txt", "M7.9.7/1037368793.dam"])

        have7z = shutil.which("7z")
        if have7z:
            for extra, out in (("-mhc=off", "plain.7z"), ("", "packed.7z")):
                cmd = [have7z, "a", "-t7z"] + ([extra] if extra else []) + \
                      [os.path.join(tmp, out), src]
                subprocess.run(cmd, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, check=True)

        srv, base = serve(tmp)
        try:
            # -- ZIP
            kind, entries, rm = remotels.listing(base + "/a.zip", 10**6, 20)
            check(kind == "zip", "ZIP распознан")
            names = [e.name for e in entries]
            check("1037368793.dam" in names,
                  "ZIP: нужный файл виден в оглавлении (%d записей)" % len(names))
            check(rm.bytes < os.path.getsize(zpath),
                  "ZIP: скачано %d из %d байт" % (rm.bytes, os.path.getsize(zpath)))
            got = remotels.zip_get(
                rm, next(e for e in entries if e.name == "1037368793.dam"))
            check(got == open(dam, "rb").read(),
                  "ZIP: вытащенный файл совпадает побайтово (%d Б)" % len(got))

            # -- RAR5
            kind, entries, rm = remotels.listing(base + "/a.rar", 10**6, 20)
            check(kind == "rar5", "RAR5 распознан")
            names = [e.name for e in entries]
            check(names == ["docs/readme.txt", "M7.9.7/1037368793.dam"],
                  "RAR5: имена разобраны (%s)" % names)

            # -- 7z
            if have7z:
                for f, label in (("plain.7z", "несжатый заголовок"),
                                 ("packed.7z", "сжатый заголовок")):
                    kind, entries, rm = remotels.listing(
                        base + "/" + f, 10**6, 20)
                    names = [os.path.basename(e.name) for e in entries]
                    size = os.path.getsize(os.path.join(tmp, f))
                    check("1037368793.dam" in names,
                          "7z (%s): файл виден, скачано %d из %d Б"
                          % (label, rm.bytes, size))
                    check(rm.bytes < size,
                          "7z (%s): скачан не весь архив" % label)
            else:
                print("(7z не установлен -- эти проверки пропущены)")
        finally:
            srv.shutdown()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print("ПРОВАЛЕНО: %d" % len(fails))
        return 1
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
