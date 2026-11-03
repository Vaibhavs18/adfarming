"""Node-free checks that web/mock/mock.js and the fixtures agree: parse the JSON, validate required keys."""
import json
import re
from pathlib import Path

from .test_build_replay import RID, br, make_src

ROOT = Path(__file__).resolve().parents[2]
MOCK_JS = (ROOT / "web" / "mock" / "mock.js").read_text(encoding="utf-8")


def test_fictional_fixtures_have_the_keys_mock_js_reads():
    fx = json.loads((ROOT / "web" / "mock" / "fixtures.json").read_text())
    for key in ("clients", "memory", "brief_text", "memory_cited_brief", "concepts", "captions", "qc_fail", "qc_pass"):
        assert key in fx
        assert f"FX.{key}" in MOCK_JS or key in ("memory", "clients")
    ids = {m["id"] for m in fx["memory"]}
    assert set(fx["memory_cited_brief"]) <= ids
    for c in fx["concepts"]:
        assert {"id", "title", "hook", "beats", "cta", "length_s", "cites"} <= set(c)


def test_replay_mode_is_wired_and_plain_mock_untouched():
    assert "get('replay')" in MOCK_JS
    assert "/replay-" in MOCK_JS and "fixtures.json" in MOCK_JS
    assert "adfarming-replay/1" in MOCK_JS  # same schema string as the builder
    assert br.SCHEMA == "adfarming-replay/1"
    # plain ?mock=1 still loads the fictional fixtures and the fictional walkthrough exists
    assert "realFetch('mock/fixtures.json')" in MOCK_JS and "function newRun(" in MOCK_JS
    # the banner text comes from the fixtures, built by the builder
    assert "Nothing here calls any AI." in br.BANNER


def test_every_field_mock_js_reads_from_replay_fixtures_is_built(tmp_path):
    fx = br.build(RID, make_src(tmp_path), tmp_path / "out", media=False)
    # fields read in mock.js: R.clients R.client R.memory R.gates[*].{step,card,answer,work_s,commentary,cost,log_q,log_a,in_play} R.tail R.artifacts R.run R.banner
    for name in re.findall(r"\bR\.([a-z_]+)", MOCK_JS):
        assert name in fx, f"mock.js reads R.{name} but the builder does not write it"
    for g in fx["gates"]:
        for name in ("step", "card", "answer", "work_s", "commentary", "cost", "log_q", "log_a", "in_play"):
            assert name in g
    for name in ("work_s", "commentary", "cost", "log"):
        assert name in fx["tail"]
    for name in ("frames", "cuts", "captions", "qc", "zip"):
        assert name in fx["artifacts"]
    for name in ("cap_usd", "total_cost_usd", "total_real_s"):
        assert name in fx["run"]
    for r in [x for g in fx["gates"] for x in g["log_q"]]:
        assert {"ts", "step", "kind", "actor", "text", "cost_usd", "memory_ids"} <= set(r)
