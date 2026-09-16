#!/usr/bin/env python3
"""
Разбор ASAP2 в дерево блоков.

Слой намеренно тупой: он не знает ни одного имени блока и не пытается
понять смысл. Его задача -- превратить поток токенов в дерево

    Block(kind, args, children)

где kind это то, что стоит после /begin, args -- все слова и строки до
первого вложенного /begin, children -- вложенные блоки.

Почему так. Стандарт ASAP2 велик, и в чужих файлах попадаются блоки,
которых мы не знаем: IF_DATA от конкретного производителя, ANNOTATION,
VAR_CRITERION и прочее. Если разбор будет знать имена, он споткнётся на
первом незнакомом. А так незнакомое просто лежит в дереве, и следующий
слой берёт из него то, что понимает.

Ключевые поля блока в ASAP2 позиционные, а не именованные, поэтому args
хранится списком в исходном порядке. Необязательные ключевые слова
(MATRIX_DIM, EXTENDED_LIMITS, FORMAT и т.д.) тоже попадают в args, и
разбирать их надо по имени, а не по позиции -- отсюда kv().
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lexer import BEGIN, END, STRING, WORD, Token, read, tokenize


@dataclass
class Block:
    kind: str
    args: list[str] = field(default_factory=list)
    children: list["Block"] = field(default_factory=list)
    line: int = 0
    # какие из args были строками в кавычках -- нужно, чтобы отличить
    # описание "16" от числа 16
    quoted: set = field(default_factory=set)

    def find(self, kind: str) -> "Block | None":
        for c in self.children:
            if c.kind == kind:
                return c
        return None

    def all(self, kind: str) -> list["Block"]:
        return [c for c in self.children if c.kind == kind]

    def kv(self, key: str, count: int = 1) -> list[str] | None:
        """
        Значения необязательного ключевого слова внутри args.

        В ASAP2 такие поля записываются как `MATRIX_DIM 12 16 1` прямо в
        теле блока, вперемешку с позиционными. Ищем по имени.
        """
        for i, a in enumerate(self.args):
            if a == key and i not in self.quoted:
                return self.args[i + 1:i + 1 + count]
        return None

    def has(self, key: str) -> bool:
        return any(a == key and i not in self.quoted
                   for i, a in enumerate(self.args))


def parse_tokens(toks: list[Token]) -> Block:
    """Корневой псевдоблок, в детях которого верхний уровень файла."""
    root = Block("ROOT")
    stack: list[Block] = [root]
    i, n = 0, len(toks)

    while i < n:
        t = toks[i]

        if t.kind == BEGIN:
            if i + 1 >= n:
                raise SyntaxError("строка %d: /begin в конце файла" % t.line)
            kind = toks[i + 1].text
            blk = Block(kind, line=t.line)
            stack[-1].children.append(blk)
            stack.append(blk)
            i += 2
            continue

        if t.kind == END:
            if len(stack) < 2:
                raise SyntaxError("строка %d: лишний /end" % t.line)
            closing = toks[i + 1].text if i + 1 < n else ""
            if closing != stack[-1].kind:
                raise SyntaxError("строка %d: /end %s закрывает %s"
                                  % (t.line, closing, stack[-1].kind))
            stack.pop()
            i += 2
            continue

        cur = stack[-1]
        if t.kind == STRING:
            cur.quoted.add(len(cur.args))
        cur.args.append(t.text)
        i += 1

    if len(stack) != 1:
        raise SyntaxError("не закрыт блок %s (строка %d)"
                          % (stack[-1].kind, stack[-1].line))
    return root


def parse_text(src: str) -> Block:
    return parse_tokens(tokenize(src))


def parse_file(path: str) -> Block:
    return parse_tokens(read(path))


def walk(blk: Block):
    """Все блоки дерева, включая сам."""
    yield blk
    for c in blk.children:
        yield from walk(c)
