#!/usr/bin/env python3
"""Export and import the Odot database as SQL.

    python3 scripts/db_io.py export --out scripts/odot-dump.sql
    python3 scripts/db_io.py import scripts/odot-dump.sql --db /path/to/other/odot.sqlite3

    python3 scripts/db_io.py export-recent --hours 12 --out scripts/recent.sql
    python3 scripts/db_io.py import-add scripts/recent.sql --db /path/to/other/odot.sqlite3

export/import move the whole database: every data table, row ids preserved, replacing
whatever the target held. export-recent/import-add move only tasks created in the last N
hours and *add* them to the target, leaving its existing rows alone; those tasks are
inserted with fresh ids so nothing can collide.

Every import copies the target to a .bak alongside it first and restores that copy if the
load fails or leaves a dangling foreign key.

All commands default to the database the app itself would use (backend/odot.sqlite3, or
ODOT_DB_PATH when set). Stdlib only, so it runs under any Python 3.9+.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
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


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def build_incremental_dump(db: sqlite3.Connection, cutoff: datetime) -> tuple[str, dict]:
    """SQL that appends recently created tasks without touching the target's own rows."""
    columns = [name for name in column_names(db, "tasks") if name != "id"]
    column_list = ", ".join(columns)
    tasks = [
        row
        for row in db.execute("SELECT * FROM tasks ORDER BY id")
        if parse_timestamp(row["created_at"]) >= cutoff
    ]
    exported_ids = {row["id"] for row in tasks}

    dependencies = db.execute(
        "SELECT task_id, depends_on_task_id FROM task_dependencies ORDER BY task_id"
    ).fetchall()
    carried = [
        row
        for row in dependencies
        if row["task_id"] in exported_ids and row["depends_on_task_id"] in exported_ids
    ]
    # A dependency with one end outside the window has no row to point at in the target.
    straddling = [
        row
        for row in dependencies
        if (row["task_id"] in exported_ids) != (row["depends_on_task_id"] in exported_ids)
    ]
    category_ids = sorted({row["category_id"] for row in tasks if row["category_id"] is not None})

    lines = [
        f"-- Odot incremental export: tasks created at or after {cutoff.isoformat()}.",
        "-- Adds rows to the target; nothing is deleted or overwritten.",
        "-- Task ids are assigned by the target, so this may be replayed into any instance.",
    ]
    if category_ids:
        lines.append(
            "-- Requires these category ids to already exist in the target: "
            + ", ".join(str(category_id) for category_id in category_ids)
        )
    lines += [
        "BEGIN TRANSACTION;",
        # Maps each source task id to the id the target just assigned, so dependencies
        # between exported tasks can be rewritten without preserving ids.
        "CREATE TEMP TABLE import_map (source_id INTEGER PRIMARY KEY, new_id INTEGER NOT NULL);",
    ]
    for row in tasks:
        values = ", ".join(sql_literal(row[column]) for column in columns)
        lines.append(f"INSERT INTO tasks ({column_list}) VALUES ({values});")
        lines.append(
            "INSERT INTO import_map (source_id, new_id) VALUES "
            f"({row['id']}, last_insert_rowid());"
        )
    for row in carried:
        lines.append(
            "INSERT INTO task_dependencies (task_id, depends_on_task_id)\n"
            "  SELECT task.new_id, depends_on.new_id FROM import_map AS task, "
            "import_map AS depends_on\n"
            f"  WHERE task.source_id = {row['task_id']} "
            f"AND depends_on.source_id = {row['depends_on_task_id']};"
        )
    lines += ["DROP TABLE import_map;", "COMMIT;"]

    summary = {
        "tasks": len(tasks),
        "dependencies": len(carried),
        "skipped_dependencies": len(straddling),
        "category_ids": category_ids,
    }
    return "\n".join(lines) + "\n", summary


def resolve_db(explicit: str | None) -> Path:
    return Path(explicit) if explicit else database_path()


def apply_sql(target: Path, sql: str) -> tuple[dict, dict, Path]:
    """Load SQL into target behind a backup; restores and exits if it does not apply cleanly."""
    # Create the schema first so a brand-new instance can be imported into directly.
    os.environ["ODOT_DB_PATH"] = str(target)
    init_db()

    backup = target.with_name(target.name + ".bak")
    shutil.copy2(target, backup)

    db = connect(target)
    before = row_counts(db)
    db.execute("PRAGMA foreign_keys = OFF")
    failure = None
    try:
        db.executescript(sql)
        problems = db.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            failure = f"foreign key check failed on {len(problems)} row(s)"
    except sqlite3.Error as exc:
        failure = str(exc)
    after = None if failure else row_counts(db)
    db.close()

    if failure:
        shutil.copy2(backup, target)
        raise SystemExit(f"Import failed ({failure}); {target} restored from {backup.name}")
    return before, after, backup


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


def cmd_export_recent(args) -> None:
    source = resolve_db(args.db)
    if not source.exists():
        raise SystemExit(f"No database at {source}")

    db = connect(source)
    missing = [table for table in EXPORTED_TABLES if not column_names(db, table)]
    if missing:
        raise SystemExit(f"{source} is missing table(s): {', '.join(missing)}")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    dump, summary = build_incremental_dump(db, cutoff)
    db.close()

    if not args.out:
        sys.stdout.write(dump)
        return

    Path(args.out).write_text(dump, encoding="utf-8")
    print(f"Exported tasks from the last {args.hours}h in {source} -> {args.out}")
    print(f"  tasks: {summary['tasks']}")
    print(f"  dependencies: {summary['dependencies']}")
    if summary["skipped_dependencies"]:
        print(
            f"  skipped {summary['skipped_dependencies']} dependency link(s) pointing outside "
            "the window"
        )
    if summary["category_ids"]:
        ids = ", ".join(str(category_id) for category_id in summary["category_ids"])
        print(f"  target must already have category id(s): {ids}")


def cmd_import(args) -> None:
    target = resolve_db(args.db)
    sql = Path(args.file).read_text(encoding="utf-8")
    _, after, backup = apply_sql(target, sql)

    print(f"Imported {args.file} -> {target}")
    for table, count in after.items():
        print(f"  {table}: {count}")
    print(f"Previous contents saved to {backup}")


def cmd_import_add(args) -> None:
    target = resolve_db(args.db)
    sql = Path(args.file).read_text(encoding="utf-8")
    before, after, backup = apply_sql(target, sql)

    print(f"Added {args.file} -> {target}")
    for table, count in after.items():
        print(f"  {table}: {before[table]} -> {count} (+{count - before[table]})")
    print(f"Previous contents saved to {backup}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="database file (defaults to the app's own)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    export = subparsers.add_parser("export", help="write all data tables as SQL")
    export.add_argument("--out", help="output path (defaults to stdout)")
    export.set_defaults(func=cmd_export)

    export_recent = subparsers.add_parser(
        "export-recent", help="write recently created tasks as additive SQL"
    )
    export_recent.add_argument("--hours", type=float, default=12, help="look-back window (12)")
    export_recent.add_argument("--out", help="output path (defaults to stdout)")
    export_recent.set_defaults(func=cmd_export_recent)

    importer = subparsers.add_parser("import", help="replace a database's data from a dump")
    importer.add_argument("file")
    importer.set_defaults(func=cmd_import)

    import_add = subparsers.add_parser(
        "import-add", help="add an export-recent dump to a database, keeping existing rows"
    )
    import_add.add_argument("file")
    import_add.set_defaults(func=cmd_import_add)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
