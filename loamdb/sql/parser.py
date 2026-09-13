"""Recursive-descent parser: token list in, an AST node out. Supports
CREATE TABLE, INSERT, SELECT (with a single INNER JOIN, WHERE, ORDER BY,
LIMIT), UPDATE, and DELETE. WHERE clauses support a flat chain of AND/OR
comparisons (no parentheses inside WHERE, kept simple deliberately).
"""
from __future__ import annotations

from . import ast
from .lexer import tokenize


class ParseError(Exception):
    pass


class Parser:
    def __init__(self, tokens: list):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos]

    def advance(self):
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def expect(self, type_):
        tok = self.peek()
        if tok.type != type_:
            raise ParseError(f"expected {type_}, got {tok.type} ({tok.value!r}) at position {tok.pos}")
        return self.advance()

    def match(self, type_) -> bool:
        if self.peek().type == type_:
            self.advance()
            return True
        return False

    def parse_statement(self):
        tok = self.peek()
        if tok.type == "CREATE":
            return self._parse_create_table()
        if tok.type == "INSERT":
            return self._parse_insert()
        if tok.type == "SELECT":
            return self._parse_select()
        if tok.type == "UPDATE":
            return self._parse_update()
        if tok.type == "DELETE":
            return self._parse_delete()
        raise ParseError(f"unexpected token {tok.type} at start of statement")

    # ---- CREATE TABLE ----

    def _parse_create_table(self):
        self.expect("CREATE"); self.expect("TABLE")
        name = self.expect("IDENT").value
        self.expect("(")
        columns = [self._parse_column_def()]
        while self.match(","):
            columns.append(self._parse_column_def())
        self.expect(")")
        return ast.CreateTable(table=name, columns=columns)

    def _parse_column_def(self):
        col_name = self.expect("IDENT").value
        type_tok = self.advance()
        if type_tok.type not in ("INTEGER", "TEXT"):
            raise ParseError(f"expected a column type (INTEGER/TEXT), got {type_tok.type}")
        is_pk = False
        if self.match("PRIMARY"):
            self.expect("KEY")
            is_pk = True
        return ast.ColumnDef(name=col_name, type=type_tok.type, primary_key=is_pk)

    # ---- INSERT ----

    def _parse_insert(self):
        self.expect("INSERT"); self.expect("INTO")
        table = self.expect("IDENT").value
        columns = []
        if self.match("("):
            columns.append(self.expect("IDENT").value)
            while self.match(","):
                columns.append(self.expect("IDENT").value)
            self.expect(")")
        self.expect("VALUES"); self.expect("(")
        values = [self._parse_literal()]
        while self.match(","):
            values.append(self._parse_literal())
        self.expect(")")
        return ast.Insert(table=table, columns=columns, values=values)

    def _parse_literal(self):
        tok = self.advance()
        if tok.type in ("INTEGER", "STRING"):
            return tok.value
        if tok.type == "NULL":
            return None
        raise ParseError(f"expected a literal value, got {tok.type} at position {tok.pos}")

    # ---- SELECT ----

    def _parse_select(self):
        self.expect("SELECT")
        columns = self._parse_select_columns()
        self.expect("FROM")
        table = self.expect("IDENT").value

        join = None
        if self.peek().type in ("JOIN", "INNER"):
            self.match("INNER")
            self.expect("JOIN")
            join_table = self.expect("IDENT").value
            self.expect("ON")
            left_col = self._parse_qualified_column()
            self.expect("OP")  # must be '='
            right_col = self._parse_qualified_column()
            join = ast.Join(table=join_table, left_column=left_col, right_column=right_col)

        where = self._parse_optional_where()
        order_by, order_desc = None, False
        if self.match("ORDER"):
            self.expect("BY")
            order_by = self._parse_qualified_column()
            if self.match("DESC"):
                order_desc = True
            elif self.match("ASC"):
                order_desc = False
        limit = None
        if self.match("LIMIT"):
            limit = self.expect("INTEGER").value
        return ast.Select(columns=columns, table=table, join=join, where=where, order_by=order_by, order_desc=order_desc, limit=limit)

    def _parse_select_columns(self):
        if self.match("*"):
            return ["*"]
        columns = [self._parse_qualified_column()]
        while self.match(","):
            columns.append(self._parse_qualified_column())
        return columns

    def _parse_qualified_column(self) -> str:
        name = self.expect("IDENT").value
        if self.match("."):
            name = name + "." + self.expect("IDENT").value
        return name

    def _parse_optional_where(self):
        if not self.match("WHERE"):
            return None
        return self._parse_bool_expr()

    def _parse_bool_expr(self):
        left = self._parse_condition()
        while self.peek().type in ("AND", "OR"):
            op = self.advance().type
            right = self._parse_condition()
            left = ast.BoolExpr(op=op, left=left, right=right)
        return left

    def _parse_condition(self):
        column = self._parse_qualified_column()
        op_tok = self.expect("OP")
        value = self._parse_literal()
        return ast.Condition(column=column, op=op_tok.value, value=value)

    # ---- UPDATE ----

    def _parse_update(self):
        self.expect("UPDATE")
        table = self.expect("IDENT").value
        self.expect("SET")
        assignments = [self._parse_assignment()]
        while self.match(","):
            assignments.append(self._parse_assignment())
        where = self._parse_optional_where()
        return ast.Update(table=table, assignments=assignments, where=where)

    def _parse_assignment(self):
        col = self.expect("IDENT").value
        self.expect("OP")
        value = self._parse_literal()
        return (col, value)

    # ---- DELETE ----

    def _parse_delete(self):
        self.expect("DELETE"); self.expect("FROM")
        table = self.expect("IDENT").value
        where = self._parse_optional_where()
        return ast.Delete(table=table, where=where)


def parse(sql: str):
    tokens = tokenize(sql)
    parser = Parser(tokens)
    stmt = parser.parse_statement()
    if parser.peek().type != "EOF":
        tok = parser.peek()
        raise ParseError(f"unexpected trailing token {tok.type} ({tok.value!r}) at position {tok.pos}")
    return stmt
