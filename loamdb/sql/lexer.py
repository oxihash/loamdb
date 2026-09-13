"""Tokenizes a SQL string into a flat list of Tokens for the parser to
consume. Single pass, no lookahead beyond one character, which is enough
for the small grammar this supports.
"""
from __future__ import annotations

KEYWORDS = {
    "CREATE", "TABLE", "PRIMARY", "KEY", "INTEGER", "TEXT",
    "INSERT", "INTO", "VALUES",
    "SELECT", "FROM", "WHERE", "AND", "OR", "NOT",
    "ORDER", "BY", "ASC", "DESC", "LIMIT",
    "UPDATE", "SET", "DELETE",
    "JOIN", "INNER", "ON", "NULL",
}


class Token:
    __slots__ = ("type", "value", "pos")

    def __init__(self, type_, value, pos):
        self.type = type_
        self.value = value
        self.pos = pos

    def __repr__(self):
        return f"Token({self.type!r}, {self.value!r})"


class LexError(Exception):
    pass


def tokenize(sql: str) -> list:
    tokens = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c.isspace():
            i += 1
            continue
        if c == "-" and i + 1 < n and sql[i + 1] == "-":  # line comment
            while i < n and sql[i] != "\n":
                i += 1
            continue
        if c.isalpha() or c == "_":
            start = i
            while i < n and (sql[i].isalnum() or sql[i] == "_"):
                i += 1
            word = sql[start:i]
            upper = word.upper()
            if upper in KEYWORDS:
                tokens.append(Token(upper, upper, start))
            else:
                tokens.append(Token("IDENT", word, start))
            continue
        if c.isdigit() or (c == "-" and i + 1 < n and sql[i + 1].isdigit()):
            start = i
            i += 1
            while i < n and sql[i].isdigit():
                i += 1
            tokens.append(Token("INTEGER", int(sql[start:i]), start))
            continue
        if c in ("'", '"'):
            quote = c
            start = i
            i += 1
            buf = []
            while i < n and sql[i] != quote:
                if sql[i] == "\\" and i + 1 < n:  # basic escape handling: \\', \\\\, etc.
                    buf.append(sql[i + 1])
                    i += 2
                else:
                    buf.append(sql[i])
                    i += 1
            if i >= n:
                raise LexError(f"unterminated string literal starting at position {start}")
            i += 1  # closing quote
            tokens.append(Token("STRING", "".join(buf), start))
            continue
        two_char = sql[i:i + 2]
        if two_char in ("<=", ">=", "!=", "<>"):
            tokens.append(Token("OP", "!=" if two_char == "<>" else two_char, i))
            i += 2
            continue
        if c in ("=", "<", ">"):
            tokens.append(Token("OP", c, i))
            i += 1
            continue
        if c in (",", "(", ")", ";", "*", "."):
            tokens.append(Token(c, c, i))
            i += 1
            continue
        raise LexError(f"unexpected character {c!r} at position {i}")
    tokens.append(Token("EOF", None, n))
    return tokens
