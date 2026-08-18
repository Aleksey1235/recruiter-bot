import sqlite3
from pathlib import Path
import ast


def _extract_schema_sql():
    source = Path(__file__).parents[1] / "database" / "db.py"
    tree = ast.parse(source.read_text("utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "SCHEMA_SQL":
                    return ast.literal_eval(node.value)
    raise AssertionError("SCHEMA_SQL not found")


def test_schema_creates_on_empty_database():
    conn = sqlite3.connect(":memory:")
    conn.executescript(_extract_schema_sql())
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"users", "shifts", "shift_members", "shift_reports", "invites", "goals", "finances", "logs", "notifications"} <= tables


def test_important_constraints_exist():
    conn = sqlite3.connect(":memory:")
    conn.executescript(_extract_schema_sql())
    conn.execute("INSERT INTO shifts (scheduled_start, scheduled_end, slots) VALUES ('2026-08-18 10:00:00', '2026-08-18 12:00:00', 1)")
    shift_id = conn.execute("SELECT id FROM shifts").fetchone()[0]
    conn.execute("INSERT INTO shift_members (shift_id, user_id) VALUES (?, ?)", (shift_id, 1))
    try:
        conn.execute("INSERT INTO shift_members (shift_id, user_id) VALUES (?, ?)", (shift_id, 1))
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("UNIQUE(shift_id, user_id) does not work")


def test_schema_rejects_invalid_report_breakdown_and_finance_state():
    schema = _extract_schema_sql()
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(schema)
    connection.execute(
        "INSERT INTO shifts(id, scheduled_start, scheduled_end, slots) VALUES(1, '2026-01-01 10:00:00', '2026-01-01 12:00:00', 1)"
    )
    connection.execute("INSERT INTO shift_members(id, shift_id, user_id) VALUES(1, 1, 1)")
    try:
        connection.execute(
            "INSERT INTO shift_reports(shift_id,member_id,user_id,total_accepted,came_to_base,found_by_recruiter) VALUES(1,1,1,3,2,2)"
        )
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("Схема должна запрещать base+self > total")

    try:
        connection.execute(
            "INSERT INTO finances(user_id,amount,type,status) VALUES(1,100,'pay','accrued')"
        )
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("Схема должна запрещать несовместимые type/status финансов")
    connection.close()
