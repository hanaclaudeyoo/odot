#!/usr/bin/env python3
"""Export and import the whole Odot database as SQL.

    python3 scripts/db_io.py export --out scripts/odot-dump.sql
    python3 scripts/db_io.py import scripts/odot-dump.sql --db /path/to/other/odot.sqlite3

Export reads every data table and writes a replayable SQL file. Import replaces all data
in the target database with the dump's contents, preserving row ids so foreign keys stay
intact. The target is copied to a .bak alongside it first, and restored if anything fails.

Both commands default to the database the app itself would use (backend/odot.sqlite3, or
ODOT_DB_PATH when set). Stdlib only, so it runs under any Python 3.9+.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.database import database_path, init_db  # noqa: E402

# Parents before children, so replaying the inserts in order satisfies foreign keys.
# active_session is omitted: an in-progress pull is UI state, not data worth cloning.
EXPORTED_TABLES = ("categories", "tasks", "task_dependencies")


def connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def column_names(db: sqlite3.Connection, table: str) -> list[str]:
    return [row["name"] for row in db.execute(f"PRAGMA table_info({table})")]


def row_counts(db: sqlite3.Connection) -> dict[str, int]:
    return {
        table: db.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]
        for table in EXPORTED_TABLES
    }


def sql_literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, bytes):
        return f"X'{value.hex()}'"
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def build_dump(db: sqlite3.Connection) -> str:
    lines = [
        "-- Odot database export.",
        "-- Replaces all data in the target database; the schema is left untouched.",
        "BEGIN TRANSACTION;",
    ]
    # Children first, so the deletes never trip a foreign key.
    for table in reversed(EXPORTED_TABLES):
        lines.append(f"DELETE FROM {table};")
    for table in EXPORTED_TABLES:
        columns = column_names(db, table)
        column_list = ", ".join(columns)
        for row in db.execute(f"SELECT * FROM {table}"):
            values = ", ".join(sql_literal(row[column]) for column in columns)
            lines.append(f"INSERT INTO {table} ({column_list}) VALUES ({values});")
    lines.append("COMMIT;")
    return "\n".join(lines) + "\n"


def resolve_db(explicit: str | None) -> Path:
    return Path(explicit) if explicit else database_path()


def cmd_export(args) -> None:
    source = resolve_db(args.db)
    if not source.exists():
        raise SystemExit(f"No database at {source}")

    db = connect(source)
    missing = [table for table in EXPORTED_TABLES if not column_names(db, table)]
    if missing:
        raise SystemExit(f"{source} is missing table(s): {', '.join(missing)}")
    dump = build_dump(db)
    counts = row_counts(db)
    db.close()

    if args.out:
        Path(args.out).write_text(dump, encoding="utf-8")
        print(f"Exported {source} -> {args.out}")
        for table, count in counts.items():
            print(f"  {table}: {count}")
    else:
        sys.stdout.write(dump)


def cmd_import(args) -> None:
    target = resolve_db(args.db)
    sql = Path(args.file).read_text(encoding="utf-8")

    # Create the schema first so a brand-new instance can be imported into directly.
    os.environ["ODOT_DB_PATH"] = str(target)
    init_db()

    backup = target.with_name(target.name + ".bak")
    shutil.copy2(target, backup)

    db = connect(target)
    db.execute("PRAGMA foreign_keys = OFF")
    failure = None
    try:
        db.executescript(sql)
        problems = db.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            failure = f"foreign key check failed on {len(problems)} row(s)"
    except sqlite3.Error as exc:
        failure = str(exc)
    counts = None if failure else row_counts(db)
    db.close()

    if failure:
        shutil.copy2(backup, target)
        raise SystemExit(f"Import failed ({failure}); {target} restored from {backup.name}")

    print(f"Imported {args.file} -> {target}")
    for table, count in counts.items():
        print(f"  {table}: {count}")
    print(f"Previous contents saved to {backup}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="database file (defaults to the app's own)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    export = subparsers.add_parser("export", help="write all data tables as SQL")
    export.add_argument("--out", help="output path (defaults to stdout)")
    export.set_defaults(func=cmd_export)

    importer = subparsers.add_parser("import", help="replace a database's data from a dump")
    importer.add_argument("file")
    importer.set_defaults(func=cmd_import)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
