"""Multi-client data layer: layout, flat backward compatibility, atomic ASR, rules parsing."""
import json
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import pytest

from adfarming import clients, cuts, footage
sys.path.insert(0, str(Path(__file__).resolve().parent))
from clientkit import fake_asr, make_client, make_clip  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def priv(tmp_path, monkeypatch):
    monkeypatch.setenv("ADF_PRIVATE", str(tmp_path / "private"))
    monkeypatch.delenv("STUDIO_CLIENT_ID", raising=False)
    monkeypatch.delenv("STUDIO_CLIENT_NAME", raising=False)
    return tmp_path / "private"


def test_flat_layout_still_works(priv):
    """No clients/ dir: the old flat layout is the single default client."""
    make_clip(priv / "footage" / "x.mov", 2)
    (priv / "asr").mkdir()
    (priv / "asr" / "x.json").write_text(json.dumps(fake_asr("flat")))
    (priv / "brief.md").write_text("FLAT BRIEF")
    assert not clients.layout_enabled()
    assert [c.id for c in clients.list_clients()] == ["dharaa"]
    assert clients.load_brief(None) == clients.load_brief("anything") == "FLAT BRIEF"
    assert clients.load_subject("dharaa") == clients.DEFAULT_SUBJECT_FLAT
    assert clients.load_refs() == ""
    cl = footage.load_clips()
    assert [c["file"] for c in cl] == ["x.mov"] and cl[0]["transcript"] == "flat"
    assert Path(cl[0]["path"]) == priv / "footage" / "x.mov"
    assert (priv / "audio").is_dir()
    # legacy clip dicts without 'path' resolve to the flat footage dir
    assert cuts.clip_path({"file": "x.mov", "duration_s": 2}) == priv / "footage" / "x.mov"


def test_two_clients_are_isolated(priv):
    make_client(priv, "alpha", "Alpha Tea", "ALPHA BRIEF", [("a1.mp4", 2), ("shared.mp4", 2)], "a chef")
    make_client(priv, "beta", "Beta Shoes", "BETA BRIEF", [("b1.mov", 3), ("shared.mp4", 3)], "a runner")
    assert clients.layout_enabled()
    assert [(c.id, c.name) for c in clients.list_clients()] == [("alpha", "Alpha Tea"), ("beta", "Beta Shoes")]
    assert clients.load_brief("alpha") == "ALPHA BRIEF" and clients.load_brief("beta") == "BETA BRIEF"
    assert clients.load_subject("beta") == "a runner"
    a, b = footage.load_clips("alpha"), footage.load_clips("beta")
    assert [c["file"] for c in a] == ["a1.mp4", "shared.mp4"]
    assert [c["file"] for c in b] == ["b1.mov", "shared.mp4"]
    assert all("/clients/alpha/" in c["path"] for c in a) and all("/clients/beta/" in c["path"] for c in b)
    # same file name, different client -> different transcript and different source file
    assert a[1]["transcript"] == "alpha says shared.mp4" and b[1]["transcript"] == "beta says shared.mp4"
    assert a[1]["duration_s"] != b[1]["duration_s"]
    cuts.speech_intervals(a[1]); cuts.speech_intervals(b[1])   # silent test audio -> no speech, but cached per path
    assert a[1]["path"] in cuts._speech_cache and b[1]["path"] in cuts._speech_cache and "shared.mp4" not in cuts._speech_cache
    # caches are per client
    assert (priv / "clients" / "alpha" / "asr" / "a1.json").exists()
    assert not (priv / "clients" / "beta" / "asr" / "a1.json").exists()


def test_unknown_and_malicious_client_ids(priv):
    make_client(priv, "alpha", "Alpha", "x", [("a.mp4", 1)])
    for bad in ("nope", "../alpha", "..", "A/B", ""):
        with pytest.raises(clients.ClientDataError):
            clients.client_dir(bad)
    assert clients.get_client("nope") is None


def test_flat_default_alongside_clients_dir(priv, monkeypatch):
    """Both layouts present: clients/<id> wins, the flat default stays reachable when it has data."""
    make_clip(priv / "footage" / "f.mov", 1)
    (priv / "brief.md").write_text("FLAT")
    make_client(priv, "alpha", "Alpha", "ALPHA", [("a.mp4", 1)])
    assert sorted(c.id for c in clients.list_clients()) == ["alpha", "dharaa"]
    assert clients.load_brief("dharaa") == "FLAT" and clients.load_brief("alpha") == "ALPHA"
    make_client(priv, "dharaa", "Dharaa", "NEW", [("d.mp4", 1)])
    assert clients.load_brief("dharaa") == "NEW"
    assert [c.id for c in clients.list_clients()].count("dharaa") == 1


def test_asr_cache_is_atomic_and_concurrent_safe(priv, tmp_path):
    d = make_client(priv, "alpha", "Alpha", "x", [("a.mp4", 2)], asr=False)
    f = d / "footage" / "a.mp4"
    (d / "asr").mkdir(); (d / "audio").mkdir()
    calls = []

    def tr(wav):
        calls.append(wav)
        assert wav.exists() and wav.name == "a.wav"
        return fake_asr("hello")

    out, errs = [], []

    def go():
        try:
            out.append(footage.asr_for(f, d / "audio", d / "asr", tr))
        except Exception as e:  # pragma: no cover
            errs.append(e)
    ts = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert not errs and all(o["transcript"] == "hello" for o in out)
    assert json.loads((d / "asr" / "a.json").read_text())["transcript"] == "hello"
    assert not [p for p in (d / "asr").iterdir() if p.name.startswith(".")]   # no temp litter
    assert not [p for p in (d / "audio").iterdir() if p.name.startswith(".")]
    n = len(calls)
    footage.asr_for(f, d / "audio", d / "asr", tr)
    assert len(calls) == n  # cached


def test_parse_rules_formats():
    items = clients.parse_rules("DO: Show the product in use\nDON'T: use year references\n+ keep it short\n"
                                "- fake urgency\nrandom line\nDont: Never mention price\n")
    assert [i["text"] for i in items] == ["Show the product in use", "Never: use year references",
                                          "keep it short", "Never: fake urgency", "Never mention price"]
    assert all(i["kind"] == "hard_rule" and i["source"] == "human" and i["status"] == "approved" for i in items)
    assert len({i["id"] for i in items}) == len(items)


def run_add(args, cwd=ROOT, stdin=""):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "add_client.py"), *args], cwd=cwd,
                          capture_output=True, text=True, input=stdin)


def test_add_client_script(priv, tmp_path):
    src = tmp_path / "pack"
    make_clip(src / "videos" / "one.mp4", 1)
    make_clip(src / "videos" / "sub" / "two.MOV", 1)
    make_clip(src / "videos" / "sub" / "one.mp4", 2)       # name clash with videos/one.mp4
    (src / "brief.txt").write_text("Sell tea to office workers.")
    (src / "rules.txt").write_text("DO: show the cup\nDON'T: promise health cures\n+ mention loose leaf\n")
    (src / "past_ads.txt").write_text("Last Diwali ad did well.")
    base = ["--id", "acme", "--name", "Acme Tea", "--from", str(src), "--no-asr", "--subject", "a tea taster",
            "--private", str(priv)]
    r = run_add(base)
    assert r.returncode == 0, r.stderr
    d = priv / "clients" / "acme"
    assert sorted(p.name for p in (d / "footage").iterdir()) == ["one.mp4", "sub_one.mp4", "two.MOV"]
    assert (d / "brief.md").read_text().strip() == "Sell tea to office workers."
    assert "Last Diwali" in (d / "refs.md").read_text()
    meta = json.loads((d / "client.json").read_text())
    assert meta["id"] == "acme" and meta["name"] == "Acme Tea" and meta["subject"] == "a tea taster"
    seed = [json.loads(l) for l in (d / "seed_memory.jsonl").read_text().splitlines()]
    assert len(seed) == 3 and all(s["kind"] == "hard_rule" and s["source"] == "human"
                                  and s["status"] == "approved" for s in seed)
    assert "ASR: skipped" in r.stdout and "Acme Tea" in r.stdout
    # refuses to overwrite without --force; --force works and loses nothing
    r2 = run_add(base)
    assert r2.returncode != 0 and "--force" in r2.stderr
    (d / "marker.txt").write_text("keep")
    assert run_add(base + ["--force"]).returncode == 0 and (d / "marker.txt").exists()
    # missing brief/subject -> placeholders when not interactive
    src2 = tmp_path / "pack2"
    make_clip(src2 / "c.mp4", 1)
    r3 = run_add(["--id", "bare", "--name", "Bare", "--from", str(src2), "--no-asr", "--private", str(priv)])
    assert r3.returncode == 0, r3.stderr
    assert "Replace everything below" in (priv / "clients" / "bare" / "brief.md").read_text()
    assert (priv / "clients" / "bare" / "subject.txt").read_text().startswith("TODO")
    assert "placeholder" in r3.stdout
    # bad ids are rejected
    assert run_add(["--id", "Bad Id", "--name", "x", "--from", str(src2)]).returncode != 0
    assert [c.id for c in clients.list_clients()] == ["acme", "bare"]
