"""Regression fixtures for the retired document path on an actual 031 schema.

New production behavior is covered by test_entry_profile_memory_{live,api,
migration}. This fixture does not turn off features on a current database.
"""
import pytest
from iris import db


def use_legacy_schema(monkeypatch):
    root=db.files('iris')
    class Scripts:
        def iterdir(self):
            return [p for p in root.joinpath('migrations').iterdir() if p.name<'032']
    class Root:
        def joinpath(self,*parts):
            return Scripts() if parts==('migrations',) else root.joinpath(*parts)
    monkeypatch.setattr(db,'files',lambda _:Root())


@pytest.fixture(autouse=True)
def legacy_schema(monkeypatch):
    use_legacy_schema(monkeypatch)
