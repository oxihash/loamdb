"""Executes a parsed AST node against a Database. SELECT reads directly
off the table B+trees (no transaction needed for a pure read); INSERT,
UPDATE, and DELETE each run inside one transaction so a mid-statement
failure (e.g. a duplicate primary key on the third row of a multi-row
UPDATE) rolls back everything that statement already staged.

Joins are a plain nested-loop join over an equality condition: for every
row in the FROM table, scan every row in the joined table looking for a
match. That is the correct fallback strategy real databases also use
once no usable index exists for a join predicate; it is not the fastest
one, which is a fine trade for a database this size.
"""
from __future__ import annotations

from .. import encoding
from ..row import Schema, encode_row, decode_row
from . import ast
from .parser import parse


class ExecutionError(Exception):
    pass


class RowContext:
    """Resolves a column reference, qualified ("table.column") or bare,
    to a value across one or more joined tables. The single-table case is
    just this with one entry, so SELECT/WHERE/ORDER BY evaluation doesn't
    need a separate code path for joins versus plain queries."""

    def __init__(self, tables: dict):
        self.tables = tables  # {table_name: (Schema, row_values)}

    def get(self, column_ref: str):
        if "." in column_ref:
            table_name, col_name = column_ref.split(".", 1)
            if table_name not in self.tables:
                raise ExecutionError(f"unknown table '{table_name}' in column reference '{column_ref}'")
            schema, row = self.tables[table_name]
            return row[schema.column_index(col_name)]
        matches = []
        for table_name, (schema, row) in self.tables.items():
            if any(c[0] == column_ref for c in schema.columns):
                matches.append((schema, row))
        if not matches:
            raise ExecutionError(f"unknown column '{column_ref}'")
        if len(matches) > 1:
            raise ExecutionError(f"ambiguous column '{column_ref}', qualify it as table.column")
        schema, row = matches[0]
        return row[schema.column_index(column_ref)]


def _compare(actual, op, expected):
    if op == "=":
        return actual == expected
    if op == "!=":
        return actual != expected
    if actual is None or expected is None:
        return False  # NULL is neither less than, greater than, nor equal to anything under < <= > >=, standard SQL semantics
    if op == "<":
        return actual < expected
    if op == "<=":
        return actual <= expected
    if op == ">":
        return actual > expected
    if op == ">=":
        return actual >= expected
    raise ExecutionError(f"unknown operator {op!r}")


def _eval_where(where, ctx: RowContext) -> bool:
    if where is None:
        return True
    if isinstance(where, ast.BoolExpr):
        if where.op == "AND":
            return _eval_where(where.left, ctx) and _eval_where(where.right, ctx)
        return _eval_where(where.left, ctx) or _eval_where(where.right, ctx)
    if isinstance(where, ast.NullCheck):
        is_null = ctx.get(where.column) is None
        return not is_null if where.negated else is_null
    return _compare(ctx.get(where.column), where.op, where.value)


def _pk_key_bytes(schema: Schema, values: list) -> bytes:
    pk_idx = schema.primary_key_index()
    pk_type = schema.columns[pk_idx][1]
    pk_value = values[pk_idx]
    if pk_value is None:
        raise ExecutionError(f"table '{schema.name}': PRIMARY KEY column cannot be NULL")
    return encoding.encode_int(pk_value) if pk_type == "INTEGER" else encoding.encode_str(pk_value)


def _load_schema(db, table: str) -> Schema:
    raw = db.get_schema(table.encode())
    if raw is None:
        raise ExecutionError(f"no such table: {table}")
    return Schema.from_bytes(raw)


def execute(db, sql: str):
    """Parses and executes one SQL statement. Returns (columns, rows) for
    SELECT, or an integer row count for INSERT/UPDATE/DELETE/CREATE TABLE
    (CREATE TABLE returns 0)."""
    stmt = parse(sql)
    if isinstance(stmt, ast.CreateTable):
        return _execute_create_table(db, stmt)
    if isinstance(stmt, ast.Insert):
        return _execute_insert(db, stmt)
    if isinstance(stmt, ast.Select):
        return _execute_select(db, stmt)
    if isinstance(stmt, ast.Update):
        return _execute_update(db, stmt)
    if isinstance(stmt, ast.Delete):
        return _execute_delete(db, stmt)
    raise ExecutionError(f"don't know how to execute {type(stmt).__name__}")


def _execute_create_table(db, stmt: ast.CreateTable) -> int:
    pk_count = sum(1 for c in stmt.columns if c.primary_key)
    if pk_count != 1:
        raise ExecutionError(f"table '{stmt.table}' must declare exactly one PRIMARY KEY column, got {pk_count}")
    if db.get_schema(stmt.table.encode()) is not None:
        raise ExecutionError(f"table '{stmt.table}' already exists")
    schema = Schema(stmt.table, [(c.name, c.type, c.primary_key) for c in stmt.columns])
    db.table_btree(stmt.table.encode())
    db.set_schema(stmt.table.encode(), schema.to_bytes())
    return 0


def _execute_insert(db, stmt: ast.Insert) -> int:
    schema = _load_schema(db, stmt.table)
    if stmt.columns:
        values = [None] * len(schema.columns)
        for col_name, val in zip(stmt.columns, stmt.values):
            values[schema.column_index(col_name)] = val
    else:
        if len(stmt.values) != len(schema.columns):
            raise ExecutionError(f"table '{stmt.table}' has {len(schema.columns)} columns but {len(stmt.values)} values were given")
        values = list(stmt.values)

    key_bytes = _pk_key_bytes(schema, values)
    row_bytes = encode_row(schema, values)

    with db.begin() as txn:
        if txn.get(stmt.table.encode(), key_bytes) is not None:
            raise ExecutionError(f"duplicate PRIMARY KEY value in table '{stmt.table}'")
        txn.insert(stmt.table.encode(), key_bytes, row_bytes)
    return 1


def _execute_select(db, stmt: ast.Select):
    schema = _load_schema(db, stmt.table)
    bt = db.table_btree(stmt.table.encode())
    contexts = []

    if stmt.join is None:
        rows_source = _select_rows_for_where(bt, schema, stmt.where)
        for row in rows_source:
            ctx = RowContext({stmt.table: (schema, row)})
            if _eval_where(stmt.where, ctx):
                contexts.append(ctx)
        all_schemas = [(stmt.table, schema)]
    else:
        right_schema = _load_schema(db, stmt.join.table)
        right_bt = db.table_btree(stmt.join.table.encode())
        right_rows = [decode_row(right_schema, v) for _, v in right_bt.scan()]
        for _, value in bt.scan():
            left_row = decode_row(schema, value)
            for right_row in right_rows:
                ctx = RowContext({stmt.table: (schema, left_row), stmt.join.table: (right_schema, right_row)})
                if ctx.get(stmt.join.left_column) == ctx.get(stmt.join.right_column) and _eval_where(stmt.where, ctx):
                    contexts.append(ctx)
        all_schemas = [(stmt.table, schema), (stmt.join.table, right_schema)]

    if stmt.order_by:
        contexts.sort(key=lambda c: _SortKey(c.get(stmt.order_by)), reverse=stmt.order_desc)
    if stmt.limit is not None:
        contexts = contexts[:stmt.limit]

    if stmt.columns == ["*"]:
        output_columns = [f"{tname}.{c[0]}" if len(all_schemas) > 1 else c[0] for tname, sch in all_schemas for c in sch.columns]
        col_refs = [f"{tname}.{c[0]}" for tname, sch in all_schemas for c in sch.columns]
    else:
        output_columns = list(stmt.columns)
        col_refs = list(stmt.columns)

    rows_out = [[ctx.get(ref) for ref in col_refs] for ctx in contexts]
    return output_columns, rows_out


def _select_rows_for_where(bt, schema: Schema, where):
    """A small, real optimization: WHERE <primary key> = <literal> can be
    answered with a single B+tree point lookup instead of a full scan."""
    if isinstance(where, ast.Condition) and where.op == "=":
        col_name = where.column.split(".")[-1]
        pk_idx = schema.primary_key_index()
        if schema.columns[pk_idx][0] == col_name:
            pk_type = schema.columns[pk_idx][1]
            key = encoding.encode_int(where.value) if pk_type == "INTEGER" else encoding.encode_str(where.value)
            value = bt.get(key)
            return [decode_row(schema, value)] if value is not None else []
    return (decode_row(schema, v) for _, v in bt.scan())


class _SortKey:
    """Makes None sort before every real value, in either direction,
    instead of raising a TypeError when Python tries to compare None
    against an int or str during sort."""
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value

    def __lt__(self, other):
        if self.value is None:
            return other.value is not None
        if other.value is None:
            return False
        return self.value < other.value

    def __eq__(self, other):
        return self.value == other.value


def _execute_update(db, stmt: ast.Update) -> int:
    schema = _load_schema(db, stmt.table)
    bt = db.table_btree(stmt.table.encode())
    pk_idx = schema.primary_key_index()

    planned = []
    for key, value in bt.scan():
        row = decode_row(schema, value)
        ctx = RowContext({stmt.table: (schema, row)})
        if not _eval_where(stmt.where, ctx):
            continue
        new_row = list(row)
        for col_name, new_val in stmt.assignments:
            new_row[schema.column_index(col_name)] = new_val
        planned.append((key, new_row))

    count = 0
    with db.begin() as txn:
        for old_key, new_row in planned:
            new_key = _pk_key_bytes(schema, new_row)
            if new_key != old_key and txn.get(stmt.table.encode(), new_key) is not None:
                raise ExecutionError(f"UPDATE would create a duplicate PRIMARY KEY value in table '{stmt.table}'")
            if new_key != old_key:
                txn.delete(stmt.table.encode(), old_key)
            txn.insert(stmt.table.encode(), new_key, encode_row(schema, new_row))
            count += 1
    return count


def _execute_delete(db, stmt: ast.Delete) -> int:
    schema = _load_schema(db, stmt.table)
    bt = db.table_btree(stmt.table.encode())

    to_delete = []
    for key, value in bt.scan():
        row = decode_row(schema, value)
        ctx = RowContext({stmt.table: (schema, row)})
        if _eval_where(stmt.where, ctx):
            to_delete.append(key)

    count = 0
    with db.begin() as txn:
        for key in to_delete:
            txn.delete(stmt.table.encode(), key)
            count += 1
    return count
