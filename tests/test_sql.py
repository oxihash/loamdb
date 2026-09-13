"""End-to-end SQL tests: real statements executed through the parser and
executor against a real Database on disk, checked against expected
results computed independently in plain Python.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from loamdb import Database, execute, ExecutionError


def fresh_db(name):
    path = name
    for ext in ("", ".wal"):
        p = path + ext
        if os.path.exists(p):
            os.remove(p)
    return Database(path), path


def cleanup(path):
    for ext in ("", ".wal"):
        p = path + ext
        if os.path.exists(p):
            os.remove(p)


def test_create_insert_select():
    db, path = fresh_db("t_basic.db")
    execute(db, "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER)")
    execute(db, "INSERT INTO users VALUES (1, 'Alice', 30)")
    execute(db, "INSERT INTO users VALUES (2, 'Bob', 25)")
    execute(db, "INSERT INTO users (id, name, age) VALUES (3, 'Carol', 35)")

    cols, rows = execute(db, "SELECT * FROM users")
    assert cols == ["id", "name", "age"]
    assert sorted(rows) == [[1, "Alice", 30], [2, "Bob", 25], [3, "Carol", 35]]

    cols, rows = execute(db, "SELECT name, age FROM users WHERE id = 2")
    assert cols == ["name", "age"]
    assert rows == [["Bob", 25]]

    db.close()
    cleanup(path)
    print("test_create_insert_select passed")


def test_duplicate_primary_key_rejected():
    db, path = fresh_db("t_dup.db")
    execute(db, "CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
    execute(db, "INSERT INTO t VALUES (1, 'a')")
    try:
        execute(db, "INSERT INTO t VALUES (1, 'b')")
        assert False, "expected a duplicate-PK ExecutionError"
    except ExecutionError:
        pass
    cols, rows = execute(db, "SELECT * FROM t")
    assert rows == [[1, "a"]]
    db.close()
    cleanup(path)
    print("test_duplicate_primary_key_rejected passed")


def test_where_operators_and_bool_logic():
    db, path = fresh_db("t_where.db")
    execute(db, "CREATE TABLE nums (id INTEGER PRIMARY KEY, val INTEGER)")
    for i in range(10):
        execute(db, f"INSERT INTO nums VALUES ({i}, {i * 10})")

    _, rows = execute(db, "SELECT id FROM nums WHERE val > 50")
    assert sorted(r[0] for r in rows) == [6, 7, 8, 9]

    _, rows = execute(db, "SELECT id FROM nums WHERE val >= 20 AND val <= 60")
    assert sorted(r[0] for r in rows) == [2, 3, 4, 5, 6]

    _, rows = execute(db, "SELECT id FROM nums WHERE val = 0 OR val = 90")
    assert sorted(r[0] for r in rows) == [0, 9]

    _, rows = execute(db, "SELECT id FROM nums WHERE val != 50")
    assert sorted(r[0] for r in rows) == [0, 1, 2, 3, 4, 6, 7, 8, 9]

    db.close()
    cleanup(path)
    print("test_where_operators_and_bool_logic passed")


def test_order_by_and_limit():
    db, path = fresh_db("t_order.db")
    execute(db, "CREATE TABLE scores (id INTEGER PRIMARY KEY, score INTEGER)")
    values = [5, 1, 9, 3, 7]
    for i, v in enumerate(values):
        execute(db, f"INSERT INTO scores VALUES ({i}, {v})")

    _, rows = execute(db, "SELECT score FROM scores ORDER BY score")
    assert [r[0] for r in rows] == sorted(values)

    _, rows = execute(db, "SELECT score FROM scores ORDER BY score DESC")
    assert [r[0] for r in rows] == sorted(values, reverse=True)

    _, rows = execute(db, "SELECT score FROM scores ORDER BY score DESC LIMIT 2")
    assert [r[0] for r in rows] == sorted(values, reverse=True)[:2]

    db.close()
    cleanup(path)
    print("test_order_by_and_limit passed")


def test_update_and_delete():
    db, path = fresh_db("t_update.db")
    execute(db, "CREATE TABLE t (id INTEGER PRIMARY KEY, status TEXT)")
    for i in range(5):
        execute(db, f"INSERT INTO t VALUES ({i}, 'pending')")

    count = execute(db, "UPDATE t SET status = 'done' WHERE id < 3")
    assert count == 3
    _, rows = execute(db, "SELECT id, status FROM t ORDER BY id")
    assert rows == [[0, "done"], [1, "done"], [2, "done"], [3, "pending"], [4, "pending"]]

    count = execute(db, "DELETE FROM t WHERE status = 'pending'")
    assert count == 2
    _, rows = execute(db, "SELECT id FROM t ORDER BY id")
    assert [r[0] for r in rows] == [0, 1, 2]

    db.close()
    cleanup(path)
    print("test_update_and_delete passed")


def test_update_changing_primary_key():
    db, path = fresh_db("t_update_pk.db")
    execute(db, "CREATE TABLE t (id INTEGER PRIMARY KEY, tag TEXT)")
    execute(db, "INSERT INTO t VALUES (1, 'a')")
    execute(db, "UPDATE t SET id = 100 WHERE id = 1")
    _, rows = execute(db, "SELECT * FROM t")
    assert rows == [[100, "a"]]
    assert execute(db, "SELECT * FROM t WHERE id = 1")[1] == []
    db.close()
    cleanup(path)
    print("test_update_changing_primary_key passed")


def test_inner_join():
    db, path = fresh_db("t_join.db")
    execute(db, "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    execute(db, "CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER, item TEXT)")
    execute(db, "INSERT INTO users VALUES (1, 'Alice')")
    execute(db, "INSERT INTO users VALUES (2, 'Bob')")
    execute(db, "INSERT INTO orders VALUES (100, 1, 'Widget')")
    execute(db, "INSERT INTO orders VALUES (101, 1, 'Gadget')")
    execute(db, "INSERT INTO orders VALUES (102, 2, 'Gizmo')")

    cols, rows = execute(db, "SELECT users.name, orders.item FROM users JOIN orders ON users.id = orders.user_id ORDER BY orders.id")
    assert cols == ["users.name", "orders.item"]
    assert rows == [["Alice", "Widget"], ["Alice", "Gadget"], ["Bob", "Gizmo"]]

    cols, rows = execute(db, "SELECT users.name, orders.item FROM users JOIN orders ON users.id = orders.user_id WHERE orders.item = 'Gizmo'")
    assert rows == [["Bob", "Gizmo"]]

    db.close()
    cleanup(path)
    print("test_inner_join passed")


def test_transactional_atomicity_on_bad_row_mid_insert():
    """A duplicate key on the SECOND of two statements inside one manual
    transaction-like sequence must not leave a half-applied insert -- here
    exercised at the SQL layer by checking each INSERT is genuinely all-
    or-nothing on its own (loamdb commits per-statement, so this verifies
    a failed statement leaves prior successful ones untouched and adds
    nothing itself)."""
    db, path = fresh_db("t_atomic.db")
    execute(db, "CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
    execute(db, "INSERT INTO t VALUES (1, 'first')")
    try:
        execute(db, "INSERT INTO t VALUES (1, 'duplicate')")
        assert False
    except ExecutionError:
        pass
    _, rows = execute(db, "SELECT * FROM t")
    assert rows == [[1, "first"]]
    db.close()
    cleanup(path)
    print("test_transactional_atomicity_on_bad_row_mid_insert passed")


def test_persistence_across_reopen():
    db, path = fresh_db("t_persist.db")
    execute(db, "CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
    for i in range(50):
        execute(db, f"INSERT INTO t VALUES ({i}, 'row{i}')")
    db.close()

    db2 = Database(path)
    _, rows = execute(db2, "SELECT * FROM t ORDER BY id")
    assert [r[0] for r in rows] == list(range(50))
    assert rows[7] == [7, "row7"]
    db2.close()
    cleanup(path)
    print("test_persistence_across_reopen passed")


def test_string_primary_key():
    db, path = fresh_db("t_strpk.db")
    execute(db, "CREATE TABLE tags (name TEXT PRIMARY KEY, count INTEGER)")
    execute(db, "INSERT INTO tags VALUES ('python', 5)")
    execute(db, "INSERT INTO tags VALUES ('rust', 2)")
    cols, rows = execute(db, "SELECT * FROM tags WHERE name = 'rust'")
    assert rows == [["rust", 2]]
    db.close()
    cleanup(path)
    print("test_string_primary_key passed")


if __name__ == "__main__":
    test_create_insert_select()
    test_duplicate_primary_key_rejected()
    test_where_operators_and_bool_logic()
    test_order_by_and_limit()
    test_update_and_delete()
    test_update_changing_primary_key()
    test_inner_join()
    test_transactional_atomicity_on_bad_row_mid_insert()
    test_persistence_across_reopen()
    test_string_primary_key()
    print("\nALL SQL TESTS PASSED")
