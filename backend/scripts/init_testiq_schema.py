"""Initialize or update TestIQ tables on the configured PostgreSQL server.

The script loads backend/.env when it exists, then runs the same schema setup
used by the API startup. It is safe to run more than once.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


backend_dir = Path(__file__).resolve().parents[1]
_load_env_file(backend_dir / ".env")
sys.path.insert(0, str(backend_dir))

from app.config import DATABASE_URL  # noqa: E402
from app.db import init_db  # noqa: E402


def main() -> None:
    if not DATABASE_URL:
        raise SystemExit("DATABASE_URL or TESTIQ_POSTGRES_HOST is required to initialize PostgreSQL tables.")
    init_db()
    print("TestIQ schema initialization completed.")


if __name__ == "__main__":
    main()
