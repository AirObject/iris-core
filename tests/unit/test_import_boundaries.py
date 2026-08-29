from pathlib import Path

from tools.check_import_boundaries import DOMAIN_ROOT, find_violations


def test_domain_tree_has_no_framework_imports() -> None:
    assert find_violations(DOMAIN_ROOT.rglob("*.py")) == ()


def test_boundary_checker_detects_an_illegal_framework_import(tmp_path: Path) -> None:
    bad_module = tmp_path / "bad_domain.py"
    bad_module.write_text("import fastapi\n", encoding="utf-8")
    violations = find_violations([bad_module])
    assert len(violations) == 1
    assert violations[0].module == "fastapi"
    assert "forbidden domain import" in violations[0].render()


def test_boundary_checker_accepts_relative_and_standard_library_imports(tmp_path: Path) -> None:
    good_module = tmp_path / "good_domain.py"
    good_module.write_text(
        "from .identifiers import ResourceId\nfrom uuid import UUID\n", encoding="utf-8"
    )
    assert find_violations([good_module]) == ()
