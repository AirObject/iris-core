"""Enforce the framework-free domain boundary."""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DOMAIN_ROOT = REPOSITORY_ROOT / "src" / "iris_memory_core" / "domain"


@dataclass(frozen=True, slots=True)
class Violation:
    path: Path
    line: int
    module: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: forbidden domain import {self.module!r}"


def _is_allowed(module: str, level: int) -> bool:
    if level > 0:
        return True
    if module == "iris_memory_core.domain" or module.startswith("iris_memory_core.domain."):
        return True
    top_level = module.partition(".")[0]
    return top_level in sys.stdlib_module_names or top_level == "__future__"


def find_violations(paths: Iterable[Path]) -> tuple[Violation, ...]:
    violations: list[Violation] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if not _is_allowed(alias.name, 0):
                        violations.append(Violation(path, node.lineno, alias.name))
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if not _is_allowed(module, node.level):
                    violations.append(Violation(path, node.lineno, module))
    return tuple(violations)


def main() -> int:
    violations = find_violations(DOMAIN_ROOT.rglob("*.py"))
    for violation in violations:
        print(violation.render())
    if violations:
        return 1
    print("domain import boundary: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
