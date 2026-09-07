"""Validate local documentation links and phase document structure."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DOCS_ROOT = REPOSITORY_ROOT / "docs"
PHASE_ROOT = DOCS_ROOT / "development"
# Markdown outside docs/ links into it too — the root README advertises the
# latest phase and its verification report.  Scanning only docs/ let the root
# README point at a report file that did not exist and still pass the gate.
EXTRA_DOCUMENT_ROOTS = (
    *REPOSITORY_ROOT.glob("*.md"),
    REPOSITORY_ROOT / "sdk",
    REPOSITORY_ROOT / "tests",
    REPOSITORY_ROOT / "hosts",
    REPOSITORY_ROOT / "web" / "console",
    REPOSITORY_ROOT / "contracts",
    REPOSITORY_ROOT / "schemas",
)
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


HEADING_PATTERN = re.compile(r"^#{1,6}\s+(.*?)\s*$")
SLUG_STRIP = re.compile(r"[^\w一-鿿\- ]")


def heading_slugs(text: str) -> frozenset[str]:
    """GitHub-style anchor slugs for every heading in one document."""
    slugs = set()
    for line in text.splitlines():
        match = HEADING_PATTERN.match(line)
        if match is None:
            continue
        slugs.add(SLUG_STRIP.sub("", match.group(1).lower()).replace(" ", "-"))
    return frozenset(slugs)


def scanned_documents() -> tuple[Path, ...]:
    """Every hand-written Markdown document this gate is responsible for."""
    documents: set[Path] = set(DOCS_ROOT.rglob("*.md"))
    for root in EXTRA_DOCUMENT_ROOTS:
        if root.is_dir():
            documents.update(root.rglob("*.md"))
        elif root.is_file():
            documents.add(root)
    excluded = {"node_modules", "dist", ".venv", "__pycache__", "test-results", "playwright-report"}
    return tuple(sorted(path for path in documents if not excluded.intersection(path.parts)))


def find_broken_local_links() -> tuple[str, ...]:
    """Validate local link targets AND their ``#anchor`` fragments.

    Checking only the path let a renamed heading silently break every deep
    link into it — and this repository links into baseline sections from
    every phase document.
    """
    broken: list[str] = []
    slug_cache: dict[Path, frozenset[str]] = {}
    for document in scanned_documents():
        text = document.read_text(encoding="utf-8")
        for raw_target in LINK_PATTERN.findall(text):
            target = raw_target.strip().strip("<>")
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            path_part, _, anchor = target.partition("#")
            path_text = unquote(path_part)
            resolved = (document.parent / path_text).resolve() if path_text else document.resolve()
            origin = document.relative_to(REPOSITORY_ROOT)
            if not resolved.exists():
                broken.append(f"{origin} -> {target}")
                continue
            if not anchor or resolved.suffix != ".md":
                continue
            if resolved not in slug_cache:
                slug_cache[resolved] = heading_slugs(resolved.read_text(encoding="utf-8"))
            if unquote(anchor) not in slug_cache[resolved]:
                broken.append(f"{origin} -> {target} (no such heading anchor)")
    return tuple(broken)


def discovered_phase_numbers() -> tuple[int, ...]:
    """Every phase that has a document, derived from the filenames.

    The gate used to hardcode ``range(3)``, so phases 3+ were never checked
    and three documents drifted out of the required structure unnoticed.
    Deriving the range from disk keeps the gate growing with the roadmap.
    """
    numbers: set[int] = set()
    for document in PHASE_ROOT.glob("phase-*.md"):
        prefix = document.name.removeprefix("phase-").partition("-")[0]
        if prefix.isdigit():
            numbers.add(int(prefix))
    return tuple(sorted(numbers))


def find_phase_structure_errors() -> tuple[str, ...]:
    errors: list[str] = []
    numbers = discovered_phase_numbers()
    if not numbers:
        return ("no phase documents found",)
    if numbers != tuple(range(len(numbers))):
        errors.append(f"phase numbering is not contiguous from 0: {list(numbers)}")
    for number in numbers:
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
