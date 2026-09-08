"""An SDK release must receive its own explicit version approval."""

from pathlib import Path

import pytest

from tools.check_release_selection import check_selection


def test_independent_distribution_versions(tmp_path: Path) -> None:
    (tmp_path / "sdk/python").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text('[project]\nname="core"\nversion="2.0.0"\n')
    (tmp_path / "sdk/python/pyproject.toml").write_text('[project]\nname="sdk"\nversion="1.0.0"\n')
    assert check_selection(tmp_path, "core", "2.0.0", upload=True) == "core==2.0.0"
    assert check_selection(tmp_path, "python-sdk", "1.0.0", upload=True) == "sdk==1.0.0"
    assert check_selection(tmp_path, "core", "", upload=False) == "core==2.0.0"
    for distribution, expected in [
        ("python-sdk", "2.0.0"),
        ("core", ""),
        ("python-sdk", ""),
        ("both", "2.0.0"),
        ("../../untrusted", "2.0.0"),
    ]:
        with pytest.raises(ValueError):
            check_selection(tmp_path, distribution, expected, upload=True)
    with pytest.raises(ValueError):
        check_selection(tmp_path, "core", "0.9.0", upload=False)
