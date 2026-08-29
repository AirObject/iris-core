"""Validate local documentation links and phase document structure."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DOCS_ROOT = REPOSITORY_ROOT / "docs"
PHASE_ROOT = DOCS_ROOT / "development"
LINK_PATTERN = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
REQUIRED_SECTIONS = {
    "阶段目标",
    "架构约束",
    "需求追踪",
    "工作包",
    "数据、契约与回退策略",
    "量化验收基线",
    "退出门禁",
    "交付证据",
    "明确不做",
    "交接条件",
}


def find_broken_local_links() -> tuple[str, ...]:
    broken: list[str] = []
    for document in sorted(DOCS_ROOT.rglob("*.md")):
        text = document.read_text(encoding="utf-8")
        for raw_target in LINK_PATTERN.findall(text):
            target = raw_target.strip().strip("<>")
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            path_text = unquote(target.partition("#")[0])
            if not path_text:
                continue
            resolved = (document.parent / path_text).resolve()
            if not resolved.exists():
                broken.append(f"{document.relative_to(REPOSITORY_ROOT)} -> {target}")
    return tuple(broken)


def find_phase_structure_errors() -> tuple[str, ...]:
    errors: list[str] = []
    for number in range(3):
        matches = tuple(PHASE_ROOT.glob(f"phase-{number:02d}-*.md"))
        if len(matches) != 1:
            errors.append(f"Phase {number} document count is {len(matches)}, expected 1")
            continue
        document = matches[0]
        text = document.read_text(encoding="utf-8")
        sections = {
            line.removeprefix("## ") for line in text.splitlines() if line.startswith("## ")
        }
        missing = sorted(REQUIRED_SECTIONS - sections)
        if missing:
            errors.append(f"{document.name} missing sections: {', '.join(missing)}")
        architecture_marker = "> 架构依据\N{FULLWIDTH COLON}"
        architecture_line = next(
            (line for line in text.splitlines() if line.startswith(architecture_marker)), ""
        )
        if "#" not in architecture_line:
            errors.append(f"{document.name} architecture references lack section anchors")
    return tuple(errors)


def main() -> int:
    errors = (*find_broken_local_links(), *find_phase_structure_errors())
    for error in errors:
        print(error)
    if errors:
        return 1
    print("documentation structure and local links: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
