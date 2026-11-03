import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
import pytest
from fastapi.testclient import TestClient

from .helpers import MP4_BYTES, PASSWORD


@pytest.fixture
def env(tmp_path, monkeypatch):
    fx = tmp_path / "fixtures"
    fx.mkdir()
    (fx / "c_9x16.mp4").write_bytes(MP4_BYTES)
    monkeypatch.setenv("ADFARMING_DB", str(tmp_path / "studio.db"))
    monkeypatch.setenv("ADFARMING_OUT", str(tmp_path / "out"))
    monkeypatch.setenv("STUDIO_PASSWORD", PASSWORD)
    monkeypatch.setenv("STUDIO_SECRET", "test-secret")
    monkeypatch.setenv("STUDIO_RUNNER", "stub")
    monkeypatch.setenv("STUDIO_STUB_FIXTURES", str(fx))
    monkeypatch.delenv("STUDIO_RUN_CAP_USD", raising=False)
    monkeypatch.delenv("STUDIO_DAILY_CAP_USD", raising=False)
    from adfarming import web
    web.set_runner(None)
    web.reset_rate_limit()
    yield tmp_path
    web.set_runner(None)


@pytest.fixture
def anon(env):
    from adfarming.web import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client(anon):
    r = anon.post("/api/login", json={"password": PASSWORD})
    assert r.status_code == 200
    return anon


