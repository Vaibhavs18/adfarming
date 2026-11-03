import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
import pytest


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ADFARMING_DB", str(tmp_path / "studio.db"))
    return tmp_path / "studio.db"
