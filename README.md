# loamdb

A relational database engine built from raw bytes up. Not a wrapper around SQLite, not an ORM. A page-based file format, a B+tree written from scratch, a write-ahead log for crash recovery, real transactions, and a SQL layer on top, all in pure Python so the whole thing is readable in an afternoon.

```sql
CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);
INSERT INTO users VALUES (1, 'Alice', 30);
SELECT name FROM users WHERE age > 25 ORDER BY age DESC;
```

## Try it

```bash
python loam.py mydata.db
```

```
loam> CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);
OK (0 rows affected)
loam> INSERT INTO users VALUES (1, 'Alice', 30);
OK (1 row affected)
loam> SELECT * FROM users;
id | name  | age
---+-------+----
1  | Alice | 30
(1 row)
```

No dependencies beyond the Python standard library.

## Architecture

Five layers, each one built and tested before the next was started.

**Pager** (`pager.py`). Fixed 4096-byte pages on a single file. Page 0 is the header: magic bytes, page count, a free list, and the catalog's root page. Freed pages are threaded into a linked list through the pages themselves, so freeing and reusing a page costs one write each way with no separate free-list structure to keep in sync.

**B+tree** (`btree.py`). Keyed on raw bytes. Leaf nodes hold the actual key/value pairs and are linked left to right for cheap ordered range scans. Internal nodes hold routing keys and child pointers only. A node is fully deserialized into a plain Python object, mutated, and reserialized on every write rather than edited in place as raw bytes, which costs some throughput in exchange for logic short enough to actually verify by reading it. Splitting propagates up to the root exactly like a textbook B+tree. Deletion removes a key from its leaf but doesn't merge underflowing leaves back together, a deliberate simplification documented in the code, not a bug.

**Write-ahead log** (`wal.py`). Every mutation is appended here and fsynced on commit before it's trusted to be durable. Records are length-prefixed, so a crash that tears a write in half is detected and the incomplete trailing record is dropped instead of being misread. On startup, recovery replays only the transactions that reached COMMIT.

**Transactions** (`database.py`). Single writer at a time, guarded by a lock, with the WAL commit as the actual durability point. This is not MVCC. It's the same trade SQLite makes in its default rollback-journal mode: give up concurrent writers for a transaction model simple enough to verify is correct.

**SQL** (`sql/`). A hand-written lexer, a recursive-descent parser, and an executor. Supports CREATE TABLE, INSERT, SELECT (with WHERE, AND/OR, ORDER BY, LIMIT, and a single INNER JOIN), UPDATE, and DELETE. A `WHERE primary_key = value` query takes a direct B+tree point lookup instead of a full scan; a join is a plain nested-loop join, which is the same fallback strategy real databases use once no index exists for the join predicate.

## What's real here and what's simplified

Real: the B+tree genuinely splits and grows across multiple levels, the WAL genuinely survives a simulated process crash (tested by writing committed records to the log and killing the process before the pages are ever touched), transactions genuinely roll back cleanly, and the SQL layer genuinely executes joins and filters against live tables, not an in-memory mock.

Simplified, on purpose: no MVCC, one writer at a time. No query planner or cost-based optimization beyond the single primary-key point-lookup shortcut. No B+tree rebalancing on delete. No indexes beyond the implicit primary key index. Floating point and DATE/TIME types aren't implemented, just INTEGER and TEXT. Every one of these is a real, well-understood extension to a system like this, left out to keep the whole thing small enough to read start to finish rather than half-implemented badly.

## Running the tests

```bash
python tests/test_btree.py
python tests/test_wal.py
python tests/test_database.py
python tests/test_sql.py
```

Each layer is tested against a reference: the B+tree against a plain Python dict across thousands of random inserts and deletes, the WAL against hand-constructed crash scenarios including a torn trailing write, the transaction layer against a real simulated crash (WAL records written and fsynced, then the process dropped before the pages are touched, then a fresh Database recovers from the log alone), and the SQL layer end to end against expected query results.

## Benchmark

On the machine this was built on, single-column integer keys, each statement its own fsynced transaction:

```
5000 inserts: 3.34s (1,495 inserts/sec)
point lookup by primary key: <1ms
full scan of 5000 rows: 16ms
```

The insert number reflects one fsync per row, which is the honest cost of the durability guarantee, not a number optimized by batching many rows into one transaction (which this benchmark deliberately doesn't do, since the SQL layer commits per statement).

## Project layout

```
loamdb/
  pager.py       page-based file storage
  btree.py       the B+tree
  wal.py         write-ahead log
  encoding.py    turns Python values into order-preserving byte keys
  row.py         row and schema serialization
  database.py    tables, transactions, catalog, crash recovery
  sql/
    lexer.py     tokenizer
    ast.py       AST node types
    parser.py    recursive-descent parser
    executor.py  runs a parsed statement against a Database
loam.py          interactive SQL shell
tests/           one test file per layer, run independently
```
