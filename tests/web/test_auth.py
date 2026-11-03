from .helpers import PASSWORD


def test_healthz_open(anon):
    assert anon.get("/healthz").json() == {"ok": True}


def test_api_requires_login(anon):
    for path in ("/api/me", "/api/clients", "/api/runs"):
        r = anon.get(path)
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "unauthorized"
    assert anon.post("/api/runs", json={"client_id": "dharaa"}).status_code == 401


def test_login_flow(anon):
    assert anon.post("/api/login", json={"password": "nope"}).status_code == 401
    assert anon.get("/api/me").status_code == 401
    r = anon.post("/api/login", json={"password": PASSWORD})
    assert r.status_code == 200
    assert "httponly" in r.headers["set-cookie"].lower()
    assert anon.get("/api/me").json() == {"ok": True}
    assert anon.get("/api/clients").json()[0]["id"] == "dharaa"


def test_forged_cookie_rejected(anon):
    anon.cookies.set("adf_session", "garbage.value.here")
    assert anon.get("/api/me").status_code == 401


def test_cookie_signed_with_other_secret_rejected(anon, monkeypatch):
    anon.post("/api/login", json={"password": PASSWORD})
    monkeypatch.setenv("STUDIO_SECRET", "another-secret")
    assert anon.get("/api/me").status_code == 401


def test_unset_password_never_logs_in(anon, monkeypatch):
    monkeypatch.delenv("STUDIO_PASSWORD")
    assert anon.post("/api/login", json={"password": ""}).status_code == 401


def test_login_rate_limit(anon):
    codes = [anon.post("/api/login", json={"password": "x"}).status_code for _ in range(6)]
    assert codes == [401] * 5 + [429]
    # even the right password is throttled now
    r = anon.post("/api/login", json={"password": PASSWORD})
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"


def test_invalid_body_is_422_with_error_shape(anon):
    r = anon.post("/api/login", json={})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"
