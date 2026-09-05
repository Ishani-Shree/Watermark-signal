"""
Apply schema.sql to the configured database.

An alternative to `psql -f schema.sql` for anyone who does not have the
Postgres client tools installed (Windows, typically). Same file, same
result -- this just runs it through the driver we already depend on.

Run from backend/ with the venv active:
    python scripts/apply_schema.py
"""

import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.db import engine  # noqa: E402

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"


def main() -> None:
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with engine.begin() as conn:
        conn.execute(text(sql))

    with engine.connect() as conn:
        tables = conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' ORDER BY table_name"
            )
        ).scalars().all()

    print(f"schema applied; {len(tables)} tables present:")
    for table in tables:
        print(f"  {table}")


if __name__ == "__main__":
    main()
