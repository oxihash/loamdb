"""AST node types produced by the parser and consumed by the executor.
Plain dataclasses, no behavior -- the executor owns all the logic for
what each node actually does.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ColumnDef:
    name: str
    type: str
    primary_key: bool = False


@dataclass
class CreateTable:
    table: str
    columns: list


@dataclass
class Insert:
    table: str
    columns: list  # column names, in the order `values` are given
    values: list


@dataclass
class Condition:
    """A single comparison: <column> <op> <literal>. Column may be
    qualified as "table.column" for joins."""
    column: str
    op: str  # =, !=, <, <=, >, >=
    value: object


@dataclass
class NullCheck:
    """<column> IS NULL, or <column> IS NOT NULL when `negated` is True."""
    column: str
    negated: bool = False


@dataclass
class BoolExpr:
    """AND/OR of two sub-expressions (Condition or another BoolExpr)."""
    op: str  # AND | OR
    left: object
    right: object


@dataclass
class Join:
    table: str
    left_column: str
    right_column: str


@dataclass
class Select:
    columns: list  # column names, or ["*"], possibly "table.column"
    table: str
    join: Optional[Join] = None
    where: Optional[object] = None  # Condition | BoolExpr | None
    order_by: Optional[str] = None
    order_desc: bool = False
    limit: Optional[int] = None


@dataclass
class Update:
    table: str
    assignments: list  # list of (column, value)
    where: Optional[object] = None


@dataclass
class Delete:
    table: str
    where: Optional[object] = None
