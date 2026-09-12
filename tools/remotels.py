#!/usr/bin/env python3
"""
remotels -- чтение оглавления удалённого архива БЕЗ его скачивания.

Задача: база дамосов лежит в облаке 240 архивами по гигабайту. Качать
240 ГБ, чтобы найти один файл, бессмысленно. Но оглавление архива -- это
килобайты, и HTTP умеет запрашивать произвольный кусок файла заголовком
Range. Значит список имён внутри архива можно прочитать, скачав от него
десятитысячную долю.

Сколько это стоит по трафику:

    ZIP   2 запроса,  ~64 КБ + размер оглавления
    7z    2 запроса,  32 байта + размер заголовка
    RAR   обход блоков, читаются только заголовки, данные пропускаются

То есть проиндексировать все 240 архивов -- это десятки мегабайт, а не
240 гигабайт. А когда нужный файл найден, `--get` вытащит только его:
ещё один Range на его собственные байты плюс распаковка на месте.

Использование:

    # что это за файл и поддерживает ли сервер Range
    python3 tools/remotels.py --probe URL

    # оглавление
    python3 tools/remotels.py URL
    python3 tools/remotels.py URL --grep '1037368793|FBH3ID60|M7\\.9\\.7'

    # список из файла со ссылками, по одной на строку
    python3 tools/remotels.py --list ссылки.txt --grep 'M7\\.9\\.7' --out найдено.txt

    # достать один файл (только ZIP: store/deflate)
    python3 tools/remotels.py URL --get 'путь/внутри/архива.dam' --dest ./

Ограничения названы честно:
  * нужен сервер, отвечающий на Range. `--probe` это проверяет первым делом;
  * `--get` реализован для ZIP. Для 7z и RAR оглавление читается, а
    распаковка одного члена требует внешней утилиты;
  * многотомный архив (part1, part2, ...) -- это ОДИН архив, разрезанный
    на куски. Оглавление такого лежит в последнем томе, а сам файл может
    пересекать границу тома. Скрипт это распознаёт и говорит прямо.
"""

from __future__ import annotations

import argparse
import os
import re
import lzma
import struct
import sys
import urllib.error
import urllib.request
import zlib

UA = "Mozilla/5.0"
TAIL = 64 * 1024


# -- доступ по кускам -------------------------------------------------------

class Remote:
    """Файл на сервере, читаемый кусками через Range."""

    def __init__(self, url: str, timeout: int = 60):
        self.url, self.timeout = url, timeout
        self.size: int | None = None
        self.ranges = False
        self.requests = 0
        self.bytes = 0
        self._cache: list[tuple[int, int, bytes]] = []

    def probe(self) -> None:
        req = urllib.request.Request(self.url, method="HEAD",
                                     headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                self.size = int(r.headers.get("Content-Length") or 0) or None
                self.ranges = "bytes" in (r.headers.get("Accept-Ranges") or "")
        except urllib.error.HTTPError:
            pass
        if self.size is None or not self.ranges:
            # не все отдают Accept-Ranges на HEAD -- пробуем реальный Range
            data, total = self._raw(0, 1)
            self.ranges = len(data) == 2 or total is not None
            if total:
                self.size = total

    def _raw(self, start: int, end: int):
        req = urllib.request.Request(
            self.url, headers={"User-Agent": UA,
                               "Range": "bytes=%d-%d" % (start, end)})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            data = r.read()
            self.requests += 1
            self.bytes += len(data)
            cr = r.headers.get("Content-Range") or ""
            total = None
            m = re.search(r"/(\d+)$", cr)
            if m:
                total = int(m.group(1))
            if r.status != 206 and start:
                raise RuntimeError(
                    "сервер не поддерживает Range (ответ %d), качать придётся "
                    "целиком" % r.status)
            return data, total

    def read(self, start: int, length: int) -> bytes:
        if length <= 0:
            return b""
        if self.size is not None:
            start = max(0, min(start, self.size - 1))
            length = min(length, self.size - start)
        for s, e, buf in self._cache:
            if s <= start and start + length <= e:
                return buf[start - s:start - s + length]
        grab = max(length, 32 * 1024)
        data, _ = self._raw(start, start + grab - 1)
        self._cache.append((start, start + len(data), data))
        if len(self._cache) > 8:
            self._cache.pop(0)
        return data[:length]

    def tail(self, n: int) -> tuple[bytes, int]:
        assert self.size is not None
        n = min(n, self.size)
        return self.read(self.size - n, n), self.size - n


# -- определение формата ----------------------------------------------------

def sniff(head: bytes) -> str:
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x06\x06", b"PK\x07\x08"):
        return "zip"
    if head[:6] == b"7z\xbc\xaf\x27\x1c":
        return "7z"
    if head[:8] == b"Rar!\x1a\x07\x01\x00":
        return "rar5"
    if head[:7] == b"Rar!\x1a\x07\x00":
        return "rar4"
    if head[:2] == b"\x1f\x8b":
        return "gzip"
    return "?"


# -- ZIP --------------------------------------------------------------------

class Entry:
    __slots__ = ("name", "size", "csize", "offset", "method")

    def __init__(self, name, size, csize, offset, method):
        self.name, self.size = name, size
        self.csize, self.offset, self.method = csize, offset, method


def zip_entries(rm: Remote) -> list[Entry]:
    buf, base = rm.tail(TAIL)
    pos = buf.rfind(b"PK\x05\x06")
    if pos < 0:
        raise RuntimeError("не нашёл конец оглавления ZIP в хвосте "
                           "-- возможно, это том многотомного архива")
    disk, cd_disk = struct.unpack_from("<HH", buf, pos + 4)
    count = struct.unpack_from("<H", buf, pos + 10)[0]
    cd_size, cd_off = struct.unpack_from("<II", buf, pos + 12)
    if disk or cd_disk:
        raise RuntimeError(
            "это том %d многотомного ZIP. Оглавление лежит в последнем томе, "
            "и файл может пересекать границу тома -- читайте последний том"
            % (disk + 1))

    z64 = buf.rfind(b"PK\x06\x07", 0, pos)
    if z64 >= 0 or cd_off == 0xFFFFFFFF or count == 0xFFFF:
        if z64 >= 0:
            eocd64_off = struct.unpack_from("<Q", buf, z64 + 8)[0]
            rec = rm.read(eocd64_off, 56)
            if rec[:4] == b"PK\x06\x06":
                count = struct.unpack_from("<Q", rec, 32)[0]
                cd_size = struct.unpack_from("<Q", rec, 40)[0]
                cd_off = struct.unpack_from("<Q", rec, 48)[0]

    cd = rm.read(cd_off, cd_size)
    out, p = [], 0
    while p + 46 <= len(cd) and cd[p:p + 4] == b"PK\x01\x02":
        method = struct.unpack_from("<H", cd, p + 10)[0]
        csize, size = struct.unpack_from("<II", cd, p + 20)
        nlen, elen, clen = struct.unpack_from("<HHH", cd, p + 28)
        off = struct.unpack_from("<I", cd, p + 42)[0]
        name = cd[p + 46:p + 46 + nlen]
        extra = cd[p + 46 + nlen:p + 46 + nlen + elen]
        if 0xFFFFFFFF in (csize, size, off):
            size, csize, off = _zip64_extra(extra, size, csize, off)
        out.append(Entry(_name(name), size, csize, off, method))
        p += 46 + nlen + elen + clen
    if count and len(out) != count and len(out) == 0:
        raise RuntimeError("оглавление не разобралось")
    return out


def _zip64_extra(extra: bytes, size, csize, off):
    p = 0
    while p + 4 <= len(extra):
        tag, ln = struct.unpack_from("<HH", extra, p)
        body = extra[p + 4:p + 4 + ln]
        if tag == 0x0001:
            q = 0
            if size == 0xFFFFFFFF and q + 8 <= len(body):
                size = struct.unpack_from("<Q", body, q)[0]
                q += 8
            if csize == 0xFFFFFFFF and q + 8 <= len(body):
                csize = struct.unpack_from("<Q", body, q)[0]
                q += 8
            if off == 0xFFFFFFFF and q + 8 <= len(body):
                off = struct.unpack_from("<Q", body, q)[0]
        p += 4 + ln
    return size, csize, off


def _name(raw: bytes) -> str:
    for enc in ("utf-8", "cp866", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def zip_get(rm: Remote, e: Entry) -> bytes:
    loc = rm.read(e.offset, 30)
    if loc[:4] != b"PK\x03\x04":
        raise RuntimeError("локальный заголовок не на месте")
    nlen, elen = struct.unpack_from("<HH", loc, 26)
    data = rm.read(e.offset + 30 + nlen + elen, e.csize)
    if e.method == 0:
        return data
    if e.method == 8:
        return zlib.decompress(data, -15)
    raise RuntimeError("метод сжатия %d не поддержан" % e.method)


# -- 7z ---------------------------------------------------------------------

def sevenz_entries(rm: Remote) -> list[Entry]:
    """
    Имена из заголовка 7z.

    Полный разбор заголовка требует пройти StreamsInfo со всеми
    PackInfo/UnpackInfo/SubStreamsInfo -- это много кода ради списка имён.
    Поэтому секция kName (0x11) ищется сканом, но каждый кандидат
    проверяется: за меткой обязан идти корректный размер, затем нулевой
    флаг внешних данных, а сам блок обязан разбираться как UTF-16LE с
    печатаемыми символами. Берётся лучший кандидат, а не первый попавшийся.
    """
    sig = rm.read(0, 32)
    nh_off, nh_size = struct.unpack_from("<QQ", sig, 12)
    if not nh_size:
        raise RuntimeError("у 7z пустой заголовок")
    hdr = rm.read(32 + nh_off, min(nh_size, 8 << 20))
    if hdr[:1] == b"\x17":
        # 7z по умолчанию сжимает заголовок. Тогда эти байты -- не имена, а
        # описание того, где лежит упакованный настоящий заголовок.
        hdr = _7z_unpack_header(rm, hdr)

    best, best_score = None, 0.0
    for i in range(len(hdr)):
        if hdr[i] != 0x11:
            continue
        try:
            size, p = _7z_num(hdr, i + 1)
        except IndexError:
            continue
        if not 4 <= size <= len(hdr) - p + 1 or p >= len(hdr):
            continue
        if hdr[p] != 0x00:              # external -- имена не здесь
            continue
        blob = hdr[p + 1:p + size]
        if len(blob) < 4 or len(blob) % 2:
            continue
        text = blob.decode("utf-16-le", "replace")
        good = sum(1 for c in text if c == "\x00" or c.isprintable())
        score = good / len(text) * len(text) ** 0.5
        if good / len(text) > 0.95 and score > best_score:
            best, best_score = text, score
    if best is None:
        raise RuntimeError("в заголовке 7z не нашлась секция имён")
    return [Entry(n, 0, 0, 0, -1) for n in best.split("\x00") if n]


_LZMA1, _LZMA2 = b"\x03\x01\x01", b"\x21"


def _7z_unpack_header(rm: Remote, blk: bytes) -> bytes:
    """
    Распаковать kEncodedHeader: разобрать StreamsInfo, забрать упакованный
    заголовок отдельным Range-запросом и распустить его LZMA.

    Разбирается типовой случай -- одна папка, один кодер. Многокодерные
    цепочки в заголовках 7z не встречаются.
    """
    p = 1
    pack_pos = pack_size = unp_size = None
    props = b""
    codec = b""
    while p < len(blk):
        kid = blk[p]
        p += 1
        if kid == 0x00:
            break
        if kid == 0x06:                                   # kPackInfo
            pack_pos, p = _7z_num(blk, p)
            npack, p = _7z_num(blk, p)
            while p < len(blk) and blk[p] != 0x00:
                if blk[p] == 0x09:                        # kSize
                    p += 1
                    for _ in range(npack):
                        pack_size, p = _7z_num(blk, p)
                else:
                    break
            p += 1
        elif kid == 0x07:                                 # kUnPackInfo
            while p < len(blk):
                sid = blk[p]
                p += 1
                if sid == 0x00:
                    break
                if sid == 0x0B:                           # kFolder
                    _nf, p = _7z_num(blk, p)
                    p += 1                                # external
                    ncod, p = _7z_num(blk, p)
                    for _ in range(ncod):
                        flags = blk[p]
                        p += 1
                        idlen = flags & 0x0F
                        codec = blk[p:p + idlen]
                        p += idlen
                        if flags & 0x10:
                            _i, p = _7z_num(blk, p)
                            _o, p = _7z_num(blk, p)
                        if flags & 0x20:
                            plen, p = _7z_num(blk, p)
                            props = blk[p:p + plen]
                            p += plen
                elif sid == 0x0C:                         # kCodersUnPackSize
                    unp_size, p = _7z_num(blk, p)
                else:
                    break
        else:
            break

    if pack_pos is None or pack_size is None:
        raise RuntimeError("не разобрал kEncodedHeader")
    data = rm.read(32 + pack_pos, pack_size)

    if codec == _LZMA1:
        if len(props) < 5:
            raise RuntimeError("короткие свойства LZMA в заголовке 7z")
        d = props[0]
        filt = [{"id": lzma.FILTER_LZMA1, "lc": d % 9, "lp": (d // 9) % 5,
                 "pb": d // 45,
                 "dict_size": struct.unpack_from("<I", props, 1)[0]}]
    elif codec == _LZMA2:
        filt = [{"id": lzma.FILTER_LZMA2}]
    else:
        raise RuntimeError(
            "заголовок 7z сжат кодером %s -- не поддержан. Скачайте архив "
            "или откройте его через py7zr" % codec.hex())

    dec = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=filt)
    out = dec.decompress(data, unp_size or (64 << 20))
    if out[:1] == b"\x17":
        raise RuntimeError("вложенный сжатый заголовок 7z")
    return out


def _7z_num(b: bytes, p: int) -> tuple[int, int]:
    first = b[p]
    p += 1
    mask, value = 0x80, 0
    for i in range(8):
        if not (first & mask):
            return value | ((first & (mask - 1)) << (8 * i)), p
        value |= b[p] << (8 * i)
        p += 1
        mask >>= 1
    return value, p


# -- RAR --------------------------------------------------------------------

def _vint(rd, pos: int) -> tuple[int, int]:
    val, shift = 0, 0
    while True:
        byte = rd(pos, 1)[0]
        pos += 1
        val |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return val, pos
        shift += 7


def rar5_entries(rm: Remote, limit: int) -> list[Entry]:
    out, pos = [], 8
    while rm.size and pos < rm.size and len(out) < limit:
        try:
            hs, p = _vint(rm.read, pos + 4)
        except (IndexError, urllib.error.HTTPError):
            break
        body = p
        blk = rm.read(body, min(hs, 1 << 16))
        q = 0
        htype, q = _vint(lambda o, n, b=blk: b[o:o + n], q)
        flags, q = _vint(lambda o, n, b=blk: b[o:o + n], q)
        extra = 0
        if flags & 0x01:
            extra, q = _vint(lambda o, n, b=blk: b[o:o + n], q)
        data = 0
        if flags & 0x02:
            data, q = _vint(lambda o, n, b=blk: b[o:o + n], q)
        if htype == 5:                       # конец архива
            break
        if htype in (2, 3):                  # файл / служебный
            _fflags, q = _vint(lambda o, n, b=blk: b[o:o + n], q)
            unp, q = _vint(lambda o, n, b=blk: b[o:o + n], q)
            _attr, q = _vint(lambda o, n, b=blk: b[o:o + n], q)
            if _fflags & 0x02:
                q += 4
            if _fflags & 0x04:
                q += 4
            _ci, q = _vint(lambda o, n, b=blk: b[o:o + n], q)
            _os, q = _vint(lambda o, n, b=blk: b[o:o + n], q)
            nlen, q = _vint(lambda o, n, b=blk: b[o:o + n], q)
            if htype == 2:
                out.append(Entry(_name(blk[q:q + nlen]), unp, data, body, -1))
        pos = body + hs + data
    return out


def rar4_entries(rm: Remote, limit: int) -> list[Entry]:
    out, pos = [], 7
    while rm.size and pos < rm.size and len(out) < limit:
        h = rm.read(pos, 11)
        if len(h) < 7:
            break
        htype = h[2]
        flags, hsize = struct.unpack_from("<HH", h, 3)
        add = struct.unpack_from("<I", h, 7)[0] if flags & 0x8000 else 0
        if hsize < 7:
            break
        if htype == 0x74:
            blk = rm.read(pos, min(hsize, 1 << 16))
            packed, unp = struct.unpack_from("<II", blk, 7)
            nlen = struct.unpack_from("<H", blk, 26)[0]
            q = 32
            if flags & 0x100:
                q += 8
            out.append(Entry(_name(blk[q:q + nlen]), unp, packed, pos, -1))
        if htype == 0x7B:
            break
        pos += hsize + add
    return out


# -- сводка -----------------------------------------------------------------

def listing(url: str, limit: int, timeout: int) -> tuple[str, list[Entry], Remote]:
    rm = Remote(url, timeout)
    rm.probe()
    if not rm.ranges:
        raise RuntimeError("сервер не отдаёт куски по Range -- этот способ "
                           "не сработает, архив придётся качать целиком")
    kind = sniff(rm.read(0, 16))
    if kind == "zip":
        return kind, zip_entries(rm), rm
    if kind == "7z":
        return kind, sevenz_entries(rm), rm
    if kind == "rar5":
        return kind, rar5_entries(rm, limit), rm
    if kind == "rar4":
        return kind, rar4_entries(rm, limit), rm
    raise RuntimeError("формат не распознан (первые байты %r)" % rm.read(0, 8))


def human(n) -> str:
    for u in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or u == "ГБ":
            return "%.0f %s" % (n, u) if u == "Б" else "%.1f %s" % (n, u)
        n /= 1024.0
    return str(n)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Оглавление удалённого архива без скачивания")
    ap.add_argument("url", nargs="?")
    ap.add_argument("--list", help="файл со ссылками, по одной на строку")
    ap.add_argument("--grep", help="регулярное выражение по именам")
    ap.add_argument("--probe", action="store_true",
                    help="только проверить Range и формат")
    ap.add_argument("--get", help="вытащить один файл по имени внутри архива")
    ap.add_argument("--dest", default=".")
    ap.add_argument("--limit", type=int, default=100000)
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--out", help="куда записать найденное")
    a = ap.parse_args(argv)

    urls = []
    if a.url:
        urls.append(a.url)
    if a.list:
        urls += [ln.strip() for ln in open(a.list, encoding="utf-8")
                 if ln.strip() and not ln.startswith("#")]
    if not urls:
        ap.error("нужен URL или --list")

    rx = re.compile(a.grep, re.I) if a.grep else None
    sink = open(a.out, "w", encoding="utf-8") if a.out else None
    total_req = total_bytes = 0
    rc = 0

    for url in urls:
        tag = url.split("/")[-1][:60] or url
        if a.probe:
            rm = Remote(url, a.timeout)
            try:
                rm.probe()
                head = rm.read(0, 16)
                print("%-60s %-6s размер %-10s Range %s"
                      % (tag, sniff(head),
                         human(rm.size) if rm.size else "?",
                         "да" if rm.ranges else "НЕТ"))
            except Exception as exc:                     # noqa: BLE001
                print("%-60s ОШИБКА %s" % (tag, exc))
                rc = 1
            continue

        try:
            kind, entries, rm = listing(url, a.limit, a.timeout)
        except Exception as exc:                         # noqa: BLE001
            print("%-60s ОШИБКА %s" % (tag, exc), file=sys.stderr)
            rc = 1
            continue

        total_req += rm.requests
        total_bytes += rm.bytes
        hits = [e for e in entries if not rx or rx.search(e.name)]
        share = (100.0 * rm.bytes / rm.size) if rm.size else 0
        print("== %s  [%s]  записей %d, подошло %d  "
              "(скачано %s из %s, %.4f %%, запросов %d)"
              % (tag, kind, len(entries), len(hits),
                 human(rm.bytes), human(rm.size or 0), share, rm.requests))
        for e in hits:
            line = "   %10s  %s" % (human(e.size) if e.size else "", e.name)
            print(line)
            if sink:
                sink.write("%s\t%s\t%d\n" % (url, e.name, e.size))

        if a.get:
            sel = [e for e in entries if e.name == a.get
                   or e.name.endswith("/" + a.get)]
            if not sel:
                print("   нет такого файла в архиве: " + a.get, file=sys.stderr)
                rc = 1
            elif kind != "zip":
                print("   --get пока только для ZIP", file=sys.stderr)
                rc = 1
            else:
                blob = zip_get(rm, sel[0])
                os.makedirs(a.dest, exist_ok=True)
                dst = os.path.join(a.dest, os.path.basename(sel[0].name))
                open(dst, "wb").write(blob)
                print("   вытащено %s -> %s" % (human(len(blob)), dst))

    if sink:
        sink.close()
        print("\nзаписано " + a.out, file=sys.stderr)
    if total_req:
        print("\nвсего: запросов %d, скачано %s"
              % (total_req, human(total_bytes)), file=sys.stderr)
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
