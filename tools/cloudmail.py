#!/usr/bin/env python3
"""
cloudmail -- список файлов публичной папки cloud.mail.ru и прямые ссылки
на них, чтобы скормить их remotels.

ВАЖНО, честно. Из среды, где это писалось, cloud.mail.ru закрыт сетевой
политикой (403 на CONNECT), поэтому обращения к их API здесь НЕ
проверялись -- проверена только логика разбора ответа. Запускать нужно с
машины, у которой есть обычный доступ в интернет. Если API поменялся,
запустите с --raw: скрипт напечатает сырой ответ, и починить разбор
будет пять минут.

Как это устроено у mail.ru:

  1. листинг публичной папки
     GET https://cloud.mail.ru/api/v2/folder?weblink=<ссылка>&limit=500
     отдаёт JSON со списком: name, size, type (file/folder), weblink

  2. адрес раздающего узла
     GET https://cloud.mail.ru/api/v2/dispatcher
     в ответе get[0].url -- база вида https://cloclo__.datacloudmail.ru/

  3. прямая ссылка на файл
     <база>weblink/view/<weblink файла>

Применение:

    # получить ссылки на все 240 архивов
    python3 tools/cloudmail.py 'RiNt/JAGmbYraM/База прошивок новая/...' \\
        --out ссылки.txt

    # прочитать оглавление каждого, не качая их
    python3 tools/remotels.py --list ссылки.txt \\
        --grep '1037368793|FBH3ID60|M7\\.9\\.7' --out найдено.txt

Ссылку берите из адресной строки браузера и отрезайте
"https://cloud.mail.ru/public/".
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request

API = "https://cloud.mail.ru/api/v2"
UA = "Mozilla/5.0"


def get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def dispatcher(timeout: int) -> str:
    raw = get("%s/dispatcher?api=2" % API, timeout)
    js = json.loads(raw)
    try:
        return js["body"]["get"][0]["url"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("не понял ответ dispatcher: " + raw[:400].decode(
            "utf-8", "replace"))


def folder(weblink: str, timeout: int, limit: int = 500) -> list[dict]:
    """Полный листинг с постраничным добором."""
    out, offset = [], 0
    while True:
        url = "%s/folder?weblink=%s&limit=%d&offset=%d" % (
            API, urllib.parse.quote(weblink, safe=""), limit, offset)
        js = json.loads(get(url, timeout))
        body = js.get("body") or {}
        items = body.get("list") or []
        out += items
        total = body.get("count", {})
        total = (total.get("folders", 0) + total.get("files", 0)) \
            if isinstance(total, dict) else len(out)
        offset += len(items)
        if not items or offset >= total:
            return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Прямые ссылки на файлы публичной папки cloud.mail.ru")
    ap.add_argument("weblink",
                    help="путь после cloud.mail.ru/public/, например "
                         "'RiNt/JAGmbYraM/папка'")
    ap.add_argument("--recurse", action="store_true",
                    help="заходить во вложенные папки")
    ap.add_argument("--ext", default="",
                    help="оставить только эти расширения, через запятую")
    ap.add_argument("--out", help="куда записать ссылки")
    ap.add_argument("--raw", action="store_true",
                    help="напечатать сырой ответ API и выйти")
    ap.add_argument("--timeout", type=int, default=30)
    a = ap.parse_args(argv)

    link = a.weblink.strip()
    for prefix in ("https://cloud.mail.ru/public/", "cloud.mail.ru/public/"):
        if link.startswith(prefix):
            link = link[len(prefix):]
    link = urllib.parse.unquote(link).strip("/")

    if a.raw:
        url = "%s/folder?weblink=%s&limit=20" % (
            API, urllib.parse.quote(link, safe=""))
        sys.stdout.write(get(url, a.timeout).decode("utf-8", "replace"))
        return 0

    base = dispatcher(a.timeout).rstrip("/") + "/"
    exts = tuple(e.strip().lower() for e in a.ext.split(",") if e.strip())

    seen, queue, rows = set(), [link], []
    while queue:
        cur = queue.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        try:
            items = folder(cur, a.timeout)
        except Exception as exc:                          # noqa: BLE001
            print("не прочитал папку %s: %s" % (cur, exc), file=sys.stderr)
            continue
        for it in items:
            wl = (it.get("weblink") or "").strip("/")
            name = it.get("name", "")
            if it.get("type") == "folder":
                if a.recurse and wl:
                    queue.append(wl)
                continue
            if exts and not name.lower().endswith(exts):
                continue
            if wl:
                rows.append((base + "weblink/view/" + wl, name,
                             it.get("size", 0)))

    sink = open(a.out, "w", encoding="utf-8") if a.out else sys.stdout
    for url, name, size in rows:
        if a.out:
            sink.write(url + "\n")
        else:
            sink.write("%s\t%s\t%d\n" % (url, name, size))
    if a.out:
        sink.close()
        print("файлов: %d, суммарно %.1f ГБ -> %s"
              % (len(rows), sum(r[2] for r in rows) / 2**30, a.out),
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
