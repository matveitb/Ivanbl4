#!/usr/bin/env python3
"""
hunt -- одна команда: пройти публичную папку с архивами дамосов и найти
те, что подходят к нашей прошивке. Ничего не скачивая целиком.

    python3 tools/hunt.py "ССЫЛКА НА ПАПКУ"

Что делает по шагам, печатая ход работы:

  1. берёт список файлов публичной папки cloud.mail.ru;
  2. у каждого архива читает ТОЛЬКО оглавление -- через HTTP Range это
     десятки килобайт вместо гигабайта;
  3. ищет в именах ключи нашего блока: номер ПО Bosch, номер железа,
     имя калибровки, номер Kia, пометки M7.9.7 и родственных моторов;
  4. складывает найденное в отчёт и говорит, что качать.

Если у вас уже есть скачанные архивы на диске -- ссылка не нужна:

    python3 tools/hunt.py --local D:\\путь\\к\\архивам

Если облако недоступно или ссылки вы собрали руками в файл:

    python3 tools/hunt.py --urls ссылки.txt
"""

from __future__ import annotations

import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import remotels                                             # noqa: E402

# Ключи ищем по именам файлов внутри архивов. Порядок -- по убыванию
# надёжности: номер ПО Bosch совпадает только у нашей же прошивки.
KEYS = [
    ("точное попадание", r"1037368793|0261208504|FBH3ID60|391102X3350"),
    ("Kia/Hyundai на M7.9.7",
     r"(?=.*(kia|hyundai|spectra|shuma|cerato|accent|getz|elantra|rio))"
     r"(?=.*(m7\.?9\.?7|10373687|10373686))"),
    ("любой M7.9.7", r"m7[._ ]?9[._ ]?7|10373687\d\d|10373686\d\d"),
    ("соседнее семейство", r"m7[._ ]?9(?![._ ]?7)|me7[._ ]?9|mse7[._ ]?9"),
]
WANTED_EXT = r"\.(dam|a2l|ols|kp)$"


def banner(text):
    print("\n" + "=" * 70)
    print(text)
    print("=" * 70)


def gather_urls(a) -> list[str]:
    if a.urls:
        return [ln.strip() for ln in open(a.urls, encoding="utf-8")
                if ln.strip() and not ln.startswith("#")]

    banner("Шаг 1. Читаю список файлов публичной папки")
    import cloudmail
    link = a.link
    for pref in ("https://cloud.mail.ru/public/", "cloud.mail.ru/public/"):
        if link.startswith(pref):
            link = link[len(pref):]
    import urllib.parse
    link = urllib.parse.unquote(link).strip("/")

    base = cloudmail.dispatcher(a.timeout).rstrip("/") + "/"
    seen, queue, urls = set(), [link], []
    while queue:
        cur = queue.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        items = cloudmail.folder(cur, a.timeout)
        for it in items:
            wl = (it.get("weblink") or "").strip("/")
            if it.get("type") == "folder":
                if wl:
                    queue.append(wl)
                continue
            if wl:
                urls.append(base + "weblink/view/" + wl)
        print("  папок пройдено %d, файлов найдено %d" % (len(seen), len(urls)))
    return urls


def scan_local(path: str):
    """Тот же поиск, но по уже скачанным архивам на диске."""
    import subprocess
    sevenz = None
    for cand in ("7z", "7za", "7z.exe", r"C:\Program Files\7-Zip\7z.exe"):
        try:
            subprocess.run([cand], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            sevenz = cand
            break
        except (OSError, subprocess.SubprocessError):
            continue
    if not sevenz:
        print("не нашёл 7-Zip. Поставьте его с 7-zip.org и повторите",
              file=sys.stderr)
        return []
    rows = []
    for root, _, files in os.walk(path):
        for f in files:
            if not re.search(r"\.(zip|7z|rar|001)$", f, re.I):
                continue
            full = os.path.join(root, f)
            try:
                r = subprocess.run([sevenz, "l", "-ba", "-slt", full],
                                   capture_output=True, text=True,
                                   errors="replace", timeout=300)
            except subprocess.SubprocessError:
                continue
            names = re.findall(r"^Path = (.+)$", r.stdout, re.M)
            rows.append((full, names))
            print("  %-50s записей %d" % (f[:50], len(names)))
    return rows


def classify(name: str):
    for label, pattern in KEYS:
        if re.search(pattern, name, re.I):
            return label
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Найти подходящий дамос, не скачивая архивы")
    ap.add_argument("link", nargs="?", help="ссылка на публичную папку")
    ap.add_argument("--urls", help="файл с готовыми ссылками")
    ap.add_argument("--local", help="папка с уже скачанными архивами")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--out", default="найдено.txt")
    a = ap.parse_args(argv)

    if not (a.link or a.urls or a.local):
        ap.error("нужна ссылка на папку, либо --urls, либо --local")

    results = []

    if a.local:
        banner("Ищу в уже скачанных архивах: " + a.local)
        for full, names in scan_local(a.local):
            for n in names:
                lbl = classify(n)
                if lbl or re.search(WANTED_EXT, n, re.I):
                    results.append((lbl or "описание калибровок", full, n, 0))
    else:
        urls = gather_urls(a)
        if not urls:
            print("список файлов пуст -- проверьте ссылку", file=sys.stderr)
            return 1

        banner("Шаг 2. Проверяю первый архив: отдаёт ли сервер куски")
        rm = remotels.Remote(urls[0], a.timeout)
        try:
            rm.probe()
        except Exception as exc:                            # noqa: BLE001
            print("  не достучался до %s\n  причина: %s\n\n"
                  "  Проверьте, что ссылка открывается в браузере и что\n"
                  "  интернет на этой машине работает без прокси."
                  % (urls[0][:80], exc), file=sys.stderr)
            return 1
        print("  %s\n  размер %s, Range %s"
              % (urls[0].split("/")[-1][:60],
                 remotels.human(rm.size or 0), "да" if rm.ranges else "НЕТ"))
        if not rm.ranges:
            print("\n  Сервер не отдаёт куски по Range. Этот способ здесь не\n"
                  "  работает -- архивы придётся качать целиком. Напишите\n"
                  "  об этом, будем думать дальше.", file=sys.stderr)
            return 1

        banner("Шаг 3. Читаю оглавления %d архивов (в %d потоков)"
               % (len(urls), a.jobs))
        import concurrent.futures as futures

        def one(url):
            try:
                kind, entries, rm = remotels.listing(url, 10 ** 6, a.timeout)
            except Exception as exc:                        # noqa: BLE001
                return url, None, [], str(exc), 0
            return url, kind, entries, None, rm.bytes

        done = 0
        total_bytes = 0
        with futures.ThreadPoolExecutor(max(1, a.jobs)) as pool:
            for url, kind, entries, err, nbytes in pool.map(one, urls):
                done += 1
                total_bytes += nbytes
                tag = url.split("/")[-1][:46]
                if err:
                    print("  [%3d/%3d] %-46s ОШИБКА %s"
                          % (done, len(urls), tag, err[:60]))
                    continue
                hits = 0
                for e in entries:
                    lbl = classify(e.name)
                    if lbl or re.search(WANTED_EXT, e.name, re.I):
                        results.append((lbl or "описание калибровок",
                                        url, e.name, e.size))
                        hits += 1
                print("  [%3d/%3d] %-46s %-5s записей %5d, подошло %d"
                      % (done, len(urls), tag, kind, len(entries), hits))
        print("\n  скачано всего: %s" % remotels.human(total_bytes))

    banner("Итог")
    if not results:
        print("Ничего не нашлось. Это тоже результат: в этой базе нашего\n"
              "блока нет. Попробуйте другую папку или пришлите список имён\n"
              "архивов -- посмотрю глазами.")
        return 0

    order = [k for k, _ in KEYS] + ["описание калибровок"]
    results.sort(key=lambda r: (order.index(r[0]) if r[0] in order else 99,
                                r[2]))
    with open(a.out, "w", encoding="utf-8") as f:
        for lbl, url, name, size in results:
            f.write("%s\t%s\t%s\t%d\n" % (lbl, url, name, size))

    shown = 0
    for lbl in order:
        rows = [r for r in results if r[0] == lbl]
        if not rows:
            continue
        print("\n-- %s: %d" % (lbl, len(rows)))
        for _l, url, name, size in rows[:15]:
            print("   %s" % name)
            print("      из %s" % url)
            shown += 1
        if len(rows) > 15:
            print("   ... ещё %d, все в %s" % (len(rows) - 15, a.out))
        if lbl == "точное попадание" and rows:
            break

    print("\nПолный список: %s (%d строк)" % (a.out, len(results)))
    print("\nДальше, для лучшего кандидата:")
    print("  python3 tools/remotels.py \"ССЫЛКА\" --get \"ИМЯ\" --dest firmware/")
    print("  python3 tools/damoscore.py firmware/ИМЯ")
    print("\nОпорное число для сравнения: наш нынешний px5ns03d.dam даёт")
    print("4 заякоренных цепочки. Кандидат интересен, если даст больше.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nпрервано", file=sys.stderr)
        sys.exit(130)
    except BrokenPipeError:
        sys.exit(0)
    except Exception as exc:                                # noqa: BLE001
        # Пользователю нужен внятный текст, а не простыня трассировки.
        print("\nНе получилось: %s\n\nЕсли причина непонятна -- пришлите эту "
              "строку, разберусь." % exc, file=sys.stderr)
        sys.exit(1)
