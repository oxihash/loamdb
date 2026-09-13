#!/usr/bin/env python3
"""Interactive SQL shell for loamdb.

Usage:
    python loam.py mydata.db
"""
from __future__ import annotations

import sys
import time

from loamdb import Database, execute, ExecutionError
from loamdb.sql.parser import ParseError


def print_table(columns, rows):
    if not rows:
        print("(0 rows)")
        return
    widths = [max(len(str(c)), max((len(str(r[i])) for r in rows), default=0)) for i, c in enumerate(columns)]
    header = " | ".join(str(c).ljust(w) for c, w in zip(columns, widths))
    print(header)
    print("-+-".join("-" * w for w in widths))
    for row in rows:
        print(" | ".join(str(v).ljust(w) for v, w in zip(row, widths)))
    print(f"({len(rows)} row{'s' if len(rows) != 1 else ''})")


def main():
    if len(sys.argv) != 2:
        print("usage: python loam.py <database-file>")
        return 1
    db = Database(sys.argv[1])
    print(f"loamdb -- connected to {sys.argv[1]}")
    print("Type SQL statements ending in ';', or .tables, or .quit")

    buffer = ""
    while True:
        try:
            line = input("loam> " if not buffer else "  ...> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        stripped = line.strip()
        if not buffer and stripped in (".quit", ".exit"):
            break
        if not buffer and stripped == ".tables":
            for name in db.list_tables():
                print(name.decode())
            continue
        buffer += line + " "
        if not stripped.endswith(";"):
            continue
        sql = buffer.strip().rstrip(";")
        buffer = ""
        start = time.monotonic()
        try:
            result = execute(db, sql)
        except (ExecutionError, ParseError) as exc:
            print(f"error: {exc}")
            continue
        elapsed = (time.monotonic() - start) * 1000
        if isinstance(result, tuple):
            columns, rows = result
            print_table(columns, rows)
        else:
            print(f"OK ({result} row{'s' if result != 1 else ''} affected)")
        print(f"[{elapsed:.2f}ms]")

    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
