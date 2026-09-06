"""Install a historical migration prefix without editing published SQL."""

from __future__ import annotations

import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from iris_memory_core.storage.migrations import MigrationRunner, default_migrations_path


def migrate_through(database: Path, version: int) -> None:
    with TemporaryDirectory(prefix="imc-historical-migrations-") as directory:
        root = Path(directory)
        for source in default_migrations_path().glob("*.sql"):
            if int(source.name[:4]) <= version:
                shutil.copy2(source, root / source.name)
        MigrationRunner(database, root).migrate()
