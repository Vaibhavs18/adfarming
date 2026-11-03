import pytest
from .helpers import MP4_BYTES, answer, new_run


def _to_cuts(client):
    rid = new_run(client)
    answer(client, rid)                         # brief
    answer(client, rid, choice="c1")            # ideas -> preview frames
    answer(client, rid)                         # preview -> edit (cuts built)
    return rid


def test_serves_artifact_with_media_type(client):
    rid = _to_cuts(client)
    r = client.get(f"/api/runs/{rid}/artifacts/c_9x16.mp4")
    assert r.status_code == 200
    assert r.headers["content-type"] == "video/mp4"
    assert r.content == MP4_BYTES
    assert client.get(f"/api/runs/{rid}/artifacts/f1.webp").headers["content-type"] == "image/webp"
    assert client.get(f"/api/runs/{rid}/artifacts/concepts.json").headers["content-type"].startswith("application/json")


def test_range_request_returns_206_exact_bytes(client):
    rid = _to_cuts(client)
    r = client.get(f"/api/runs/{rid}/artifacts/c_9x16.mp4", headers={"Range": "bytes=100-199"})
    assert r.status_code == 206
    assert r.content == MP4_BYTES[100:200]
    assert r.headers["content-range"] == f"bytes 100-199/{len(MP4_BYTES)}"
    r = client.get(f"/api/runs/{rid}/artifacts/c_9x16.mp4", headers={"Range": "bytes=-16"})
    assert r.status_code == 206 and r.content == MP4_BYTES[-16:]


@pytest.mark.parametrize("name", [
    "..%2f..%2fstudio.db", "%2e%2e%2fstudio.db", "..%252f..%252fstudio.db", "%2e%2e/studio.db",
    "....//studio.db", "..%5cstudio.db", "a%00.mp4", "../c_9x16.mp4", ".hidden.mp4",
    "nonexistent.mp4", "c_9x16.mp4%2f", "%2Fetc%2Fpasswd"])
def test_traversal_rejected(client, env, name):
    rid = _to_cuts(client)
    (env / "secret.mp4").write_bytes(b"outside")
    (env / "out" / "secret.mp4").write_bytes(b"sibling")
    r = client.get(f"/api/runs/{rid}/artifacts/{name}")
    assert r.status_code in (404, 400), (name, r.status_code)
    assert b"outside" not in r.content and b"sibling" not in r.content


def test_only_flat_files_and_known_extensions(client, env):
    rid = _to_cuts(client)
    d = env / "out" / rid
    (d / "sub").mkdir()
    (d / "sub" / "x.mp4").write_bytes(b"x")
    (d / "notes.py").write_text("print(1)")
    assert client.get(f"/api/runs/{rid}/artifacts/sub").status_code == 404
    assert client.get(f"/api/runs/{rid}/artifacts/sub/x.mp4").status_code == 404
    assert client.get(f"/api/runs/{rid}/artifacts/notes.py").status_code == 404


def test_symlink_escape_rejected(client, env):
    rid = _to_cuts(client)
    outside = env / "outside.mp4"
    outside.write_bytes(b"outside-data")
    (env / "out" / rid / "link.mp4").symlink_to(outside)
    assert client.get(f"/api/runs/{rid}/artifacts/link.mp4").status_code == 404


def test_run_id_must_be_server_uuid(client, env):
    new_run(client)
    for bad in ("..", "r_abc", "../out", "%2e%2e", "00000000-0000-0000-0000-000000000000"):
        assert client.get(f"/api/runs/{bad}/artifacts/c_9x16.mp4").status_code == 404
        assert client.get(f"/api/runs/{bad}").status_code == 404


def test_artifacts_need_login(client, anon):
    rid = _to_cuts(client)
    client.cookies.clear()
    assert client.get(f"/api/runs/{rid}/artifacts/c_9x16.mp4").status_code == 401


def test_private_dir_never_served(client, env, monkeypatch):
    from adfarming import web
    rid = _to_cuts(client)
    assert web.PRIVATE_DIR.name == "private"
    # even if ADFARMING_OUT pointed inside private/, the file must not be served
    monkeypatch.setenv("ADFARMING_OUT", str(web.PRIVATE_DIR / "nope"))
    assert client.get(f"/api/runs/{rid}/artifacts/c_9x16.mp4").status_code == 404


def test_adf_private_and_db_dir_never_served(client, env, monkeypatch):
    import os
    from .helpers import new_run
    priv = env / "adfpriv"
    priv.mkdir()
    (priv / "secret.json").write_text('{"x":1}')
    monkeypatch.setenv("ADF_PRIVATE", str(priv))
    rid = new_run(client)
    d = env / "out" / rid
    d.mkdir(parents=True, exist_ok=True)
    os.symlink(priv / "secret.json", d / "leak.json")
    assert client.get(f"/api/runs/{rid}/artifacts/leak.json").status_code == 404
    # symlink to the DB file (lives next to out/ in tests) is refused too
    os.symlink(env / "studio.db", d / "db.json")
    assert client.get(f"/api/runs/{rid}/artifacts/db.json").status_code == 404
    # a normal artifact still works, even though out/ sits inside the DB directory here
    (d / "ok.json").write_text("{}")
    assert client.get(f"/api/runs/{rid}/artifacts/ok.json").status_code == 200
