#!/usr/bin/env python3
"""
Токенизатор ASAP2.

Формат простой, но ловушек в нём три, и все они встречались в реальных
файлах:

  * строка в кавычках может содержать что угодно, включая слэши и
    вертикальную черту -- в наших описаниях карт стоит "оценка | текст",
    и наивное разбиение по пробелам их рвёт;
  * комментарии бывают двух видов, /* */ и //, причём открывающая
    последовательность может оказаться ВНУТРИ строки в кавычках, где она
    комментарием не является;
  * переводы строк бывают и CRLF, и голые LF. Наш собственный файл --
    целиком CRLF, чужие обычно LF.

Поэтому разбор посимвольный, а не регулярками: только так состояние
"мы внутри кавычек" отслеживается надёжно.

Числа НЕ разбираются здесь. Токенизатор отдаёт их текстом, потому что в
ASAP2 одно и то же поле бывает то целым, то с плавающей точкой, то
шестнадцатеричным, и решать это должен разбор блока, знающий смысл поля.
"""

from __future__ import annotations

from dataclasses import dataclass

BEGIN = "BEGIN"
END = "END"
WORD = "WORD"
STRING = "STRING"


@dataclass(frozen=True)
class Token:
    kind: str          # BEGIN, END, WORD, STRING
    text: str          # для STRING -- уже без кавычек
    line: int


def tokenize(src: str) -> list[Token]:
    out: list[Token] = []
    i, n, line = 0, len(src), 1

    while i < n:
        ch = src[i]

        if ch == "\n":
            line += 1
            i += 1
            continue
        if ch in " \t\r":
            i += 1
            continue

        # комментарии -- только вне строки, сюда мы попадаем именно так
        if src.startswith("/*", i):
            end = src.find("*/", i + 2)
            if end < 0:
                raise SyntaxError("строка %d: не закрыт комментарий /*" % line)
            line += src.count("\n", i, end)
            i = end + 2
            continue
        if src.startswith("//", i):
            end = src.find("\n", i)
            i = n if end < 0 else end
            continue

        # /begin и /end -- отдельные токены, а не слова
        if src.startswith("/begin", i):
            out.append(Token(BEGIN, "/begin", line))
            i += 6
            continue
        if src.startswith("/end", i):
            out.append(Token(END, "/end", line))
            i += 4
            continue

        if ch == '"':
            i += 1
            buf = []
            start_line = line
            while i < n:
                c = src[i]
                if c == "\\" and i + 1 < n:
                    # экранирование: в наших файлах не встречается, но
                    # стандарт его допускает
                    buf.append(src[i + 1])
                    i += 2
                    continue
                if c == '"':
                    i += 1
                    break
                if c == "\n":
                    line += 1
                buf.append(c)
                i += 1
            else:
                raise SyntaxError("строка %d: не закрыта кавычка" % start_line)
            out.append(Token(STRING, "".join(buf), start_line))
            continue

        # обычное слово: до пробела или начала строки в кавычках
        j = i
        while j < n and src[j] not in ' \t\r\n"':
            if src.startswith("/*", j) or src.startswith("//", j):
                break
            j += 1
        if j == i:
            j = i + 1
        out.append(Token(WORD, src[i:j], line))
        i = j

    return out


def read(path: str) -> list[Token]:
    """Прочитать файл и разобрать. Кодировка у A2L бывает разной."""
    raw = open(path, "rb").read()
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return tokenize(raw.decode(enc))
        except UnicodeDecodeError:
            continue
    return tokenize(raw.decode("latin-1", "replace"))
