"""Composition planner: schema, lint rules, repair, retry loop, fallback. Synthetic data, no network."""
import copy
import json

import pytest
from jsonschema import Draft202012Validator

from adfarming import compose_plan as cp

SCHEMA = json.loads((cp.ROOT / "schemas/composition_plan.schema.json").read_text())
VAL = Draft202012Validator(SCHEMA)

CONCEPT = {
    "id": "c1", "title": "Test Story", "hook": "spoken hook", "cta": "See the kurta on our website",
    "beats": [
        {"clip": "a.mov", "src_in": 1.0, "src_out": 5.0, "purpose": "hook", "on_screen_text": "Worried about fit?"},
        {"clip": "b.mov", "src_in": 2.0, "src_out": 6.0, "purpose": "agitate", "on_screen_text": "Not sure?"},
        {"clip": "a.mov", "src_in": 8.0, "src_out": 11.0, "purpose": "cta", "on_screen_text": "See it"},
    ],
}  # output windows: 0-4, 4-8, 8-11 -> duration 11
ASSETS = {"backdrops": [{"name": "wall.png", "description": "plain wall", "has_people": False},
                        {"name": "crowd.png", "description": "street", "has_people": True}],
          "pips": [{"name": "card.png", "description": "fabric close-up"}]}
RULES = ["No hard-sell and no fake urgency.", "Never invent prices, discounts or fabric facts."]


def good():
    p = cp.default_plan(CONCEPT, ["#5a1230", "#a3321f"])
    return cp.repair_plan(p, [], CONCEPT, assets_info=ASSETS)


def codes(plan, **kw):
    return {p["code"] for p in cp.lint_plan(plan, CONCEPT, RULES, assets_info=ASSETS, **kw) if p["level"] == "error"}


def test_schema_file_is_valid_and_catalog_matches():
    Draft202012Validator.check_schema(SCHEMA)
    assert set(SCHEMA["properties"]["preset"]["enum"]) == set(cp.PRESETS)
    assert set(SCHEMA["properties"]["text"]["items"]["properties"]["style"]["enum"]) == set(cp.TEXT_STYLES)
    assert set(SCHEMA["properties"]["text"]["items"]["properties"]["region"]["enum"]) == set(cp.REGION_CAP)


def test_default_plan_validates_and_lints_clean():
    p = cp.default_plan(CONCEPT, ["#5a1230", "#a3321f"])
    assert not list(VAL.iter_errors(p))
    assert codes(p) == set()
    assert p["duration"] == 11.0 and p["meta"]["source"] == "default"
    roles = {t["role"]: t for t in p["text"]}
    assert roles["hook"]["text"] == "Worried about fit?" and roles["cta"]["text"] == CONCEPT["cta"]
    assert roles["cta"]["end"] <= 11.0 and roles["cta"]["start"] >= 8.0


def test_default_plan_with_no_brand_colours_and_plate_look():
    p = cp.default_plan(CONCEPT, None, look="cutout_on_textile_plate")
    assert not list(VAL.iter_errors(p)) and codes(p) == set()
    assert p["preset"] == "cutout_on_textile_plate"
    assert cp.backdrop_requests(p)


def test_lint_catches_each_rule():
    base = good()
    def mod(f):
        p = copy.deepcopy(base)
        f(p)
        return p
    # people still / unknown still
    assert "backdrop" in codes(mod(lambda p: p["background"][0].update(kind="blur_still", src="crowd.png")))
    assert "backdrop" in codes(mod(lambda p: p["background"][0].update(kind="image", src="nope.png")))
    # timing outside the reel
    assert "timing" in codes(mod(lambda p: p["text"][0].update(end=30.0)))
    # locked text altered
    assert "locked" in codes(mod(lambda p: [t.update(text="Buy now cheaper") for t in p["text"] if t["role"] == "cta"]))
    assert "locked" in codes(mod(lambda p: [t.update(text="Other hook") for t in p["text"] if t["role"] == "hook"]))
    # too long for region
    assert "text_len" in codes(mod(lambda p: p["text"].append(
        dict(p["text"][0], id="c1", role="callout", region="left_mid", style="callout_tag",
             text="x" * 40, start=4.5, end=6.5, beat=1, plate="#fbbf24", fill="#111111"))))
    # price / discount
    assert "price" in codes(mod(lambda p: p["text"].append(
        dict(p["text"][0], id="c1", role="callout", region="left_mid", style="callout_tag",
             text="Only Rs 499 today", start=4.5, end=6.5, beat=1))))
    # urgency
    assert "urgency" in codes(mod(lambda p: p["text"].append(
        dict(p["text"][0], id="c1", role="callout", region="left_mid", style="callout_tag",
             text="Hurry, last chance", start=4.5, end=6.5, beat=1))))
    # contrast: dark text on dark bg
    assert "contrast" in codes(mod(lambda p: p["text"][0].update(fill="#2a0a1a", stroke=None)))
    # scrim
    assert "scrim" in codes(mod(lambda p: p["captions"].update(scrim=0.1)))
    # plate not dimmed
    def plate(p):
        p["background"][0].update(kind="generated", prompt="weave", dim=0.95)
        p["preset"] = "cutout_on_textile_plate"
    assert "plate_dim" in codes(mod(plate))
    # people in plate prompt
    def pp(p):
        p["background"][0].update(kind="generated", prompt="a woman in a saree", dim=0.5)
        p["preset"] = "cutout_on_textile_plate"
    assert "plate_prompt" in codes(mod(pp))
    # segments mismatch, duration mismatch
    assert "segments" in codes(mod(lambda p: p["subject"]["segments"][0].update(**{"in": 0.0})))
    assert "duration" in codes(mod(lambda p: p.update(duration=15.0)))
    # preset mismatch
    assert "preset" in codes(mod(lambda p: p.update(preset="split_with_pip_card")))
    # schema error (unknown region)
    assert "schema" in codes(mod(lambda p: p["text"][0].update(region="middle")))
    # overlay unknown asset
    assert "overlay_src" in codes(mod(lambda p: p.update(overlays=[{
        "id": "pip", "src": "asset:ghost.png", "region": "right_mid", "size": [270, 350], "rot": 3,
        "animation": "slide_right", "start": 5.0, "end": 8.0}], preset="split_with_pip_card")))


def test_max_two_text_blocks_visible_including_captions():
    p = good()
    for i, (s, e) in enumerate([(0.5, 3.0), (0.5, 3.0)]):
        p["text"].append(dict(p["text"][0], id=f"x{i}", role="callout", region=["left_mid", "right_mid"][i],
                              style="callout_tag", text="Short tag", start=s, end=e, beat=0, plate="#fbbf24", fill="#111111"))
    assert "too_much_text" in codes(p)
    fixed = cp.repair_plan(p, [], CONCEPT, assets_info=ASSETS)
    assert "too_much_text" not in codes(fixed)
    assert {t["role"] for t in fixed["text"]} >= {"hook", "cta"}  # locked blocks survive


def test_repair_fixes_timings_text_and_backdrops():
    p = good()
    p["text"][0]["start"], p["text"][0]["end"] = -2.0, 99.0
    p["background"][0].update(kind="blur_still", src="crowd.png", blur=15, dim=1.0)
    p["preset"] = "cutout_over_blurred_scene"
    p["text"].append(dict(p["text"][0], id="c1", role="callout", region="left_mid", style="callout_tag",
                          text="this callout is much too long for a small side region", start=4.5, end=7.0, beat=1,
                          plate="#fbbf24", fill="#111111"))
    p["captions"]["scrim"] = 0.0
    p["text"][1].update(text="Tampered cta")
    fixed = cp.repair_plan(p, cp.lint_plan(p, CONCEPT, RULES, assets_info=ASSETS), CONCEPT, assets_info=ASSETS)
    assert codes(fixed) == set()
    assert not list(VAL.iter_errors(fixed))
    hook = next(t for t in fixed["text"] if t["role"] == "hook")
    assert 0 <= hook["start"] < hook["end"] <= 4.0  # clamped into beat 0
    assert fixed["background"][0]["src"] == "wall.png"  # people still swapped for the person-free one
    callout = next(t for t in fixed["text"] if t["role"] == "callout")
    assert len(callout["text"]) <= cp.REGION_CAP["left_mid"]
    assert next(t for t in fixed["text"] if t["role"] == "cta")["text"] == CONCEPT["cta"]
    assert fixed["captions"]["scrim"] >= 0.35
    assert fixed["meta"]["repaired"]


def test_repair_without_assets_turns_people_still_into_gradient():
    p = good()
    p["background"][0].update(kind="image", src="crowd.png", dim=0.5)
    fixed = cp.repair_plan(p, [], CONCEPT, assets_info=None)
    assert fixed["background"][0]["kind"] == "gradient" and fixed["preset"] == "cutout_on_brand_colour"


def test_repair_fixes_contrast_and_removes_price_blocks():
    p = good()
    p["text"][0].update(fill="#2a0a1a", stroke=None)
    p["text"].append(dict(p["text"][0], id="c1", role="callout", region="left_mid", style="callout_tag",
                          text="20% off today", start=4.5, end=6.5, beat=1, fill="#ffffff"))
    fixed = cp.repair_plan(p, [], CONCEPT, assets_info=ASSETS)
    assert codes(fixed) == set()
    assert not any("off" in t["text"] for t in fixed["text"])
    assert next(t for t in fixed["text"] if t["role"] == "hook")["fill"] in ("#ffffff", "#111111")


def test_price_allowed_when_concept_gave_it():
    c = copy.deepcopy(CONCEPT)
    c["beats"][1]["on_screen_text"] = "Rs 999 only"
    p = cp.default_plan(c, None)
    p["text"].append(dict(p["text"][0], id="c1", role="callout", region="left_mid", style="callout_tag",
                          text="Rs 999 only", start=4.5, end=6.5, beat=1))
    assert not [x for x in cp.lint_plan(p, c, RULES) if x["code"] == "price"]


def test_locked_cta_overlong_keeps_text_and_fits_by_size():
    c = copy.deepcopy(CONCEPT)
    c["cta"] = "If you are new to sarees and kurtas, do come and see the whole collection on our website today"
    p = cp.default_plan(c, None)
    p = cp.repair_plan(p, [], c)
    cta = next(t for t in p["text"] if t["role"] == "cta")
    assert cta["text"] == c["cta"] and cta["size"] < 64


def test_backdrop_requests_are_person_free_and_tinted():
    p = good()
    p["background"].append({"id": "plate1", "kind": "generated", "colors": ["#5a1230", "#a3321f"], "src": None,
                            "prompt": "woven handloom weave with a woman, text logo", "blur": 0, "zoom": 1.1,
                            "dim": 0.5, "start": 4.0, "transition_in": {"type": "fade", "dur": 0.6}})
    p["preset"] = "cutout_on_textile_plate"
    reqs = cp.backdrop_requests(p)
    assert len(reqs) == 1 and reqs[0]["name"] == "plate1"
    low = reqs[0]["prompt"].lower()
    assert "no people" in low and "no text" in low and "#5a1230" in low
    assert "a woman" not in low and "logo" not in low.split("no ")[0]
    assert cp.backdrop_requests(good()) == []


def test_plan_summary_bullets():
    s = cp.plan_summary(good())
    assert any(x.startswith("Background:") for x in s)
    assert any(x.startswith("Headline: Worried about fit?") for x in s)
    assert any(x.startswith("CTA:") for x in s)


def test_hard_rules_from_memory():
    mem = "- [brand_voice] warm\n- [hard_rule] No fake urgency.\n- [preference] x\n- [hard_rule] Never invent prices."
    assert cp.hard_rules_from_memory(mem) == ["No fake urgency.", "Never invent prices."]


# ----------------------------------------------------------------- retry loop with a fake spark
class Fake:
    def __init__(self, outputs):
        self.outputs, self.prompts = list(outputs), []

    def __call__(self, system, user, effort="low", max_out=8000):
        self.prompts.append(user)
        o = self.outputs.pop(0)
        if isinstance(o, Exception):
            raise o
        return o, {"model": "fake", "tokens": 10}


def run_plan(fake, **kw):
    return cp.plan_composition(None, CONCEPT, "brief text", "- [hard_rule] No fake urgency.", ASSETS, spark=fake, **kw)


def test_plan_composition_accepts_clean_model_plan():
    fake = Fake([good()])
    p = run_plan(fake, guidance="calmer background")
    assert p["meta"]["source"] == "spark" and p["meta"]["attempts"] == 1
    assert "calmer background" in fake.prompts[0] and "Worried about fit?" in fake.prompts[0]
    assert not list(VAL.iter_errors(p)) and codes(p) == set()


def test_plan_composition_repairs_mechanical_problems_without_retry():
    bad = good()
    bad["text"][0]["end"] = 50.0
    bad["captions"]["scrim"] = 0
    fake = Fake([bad])
    p = run_plan(fake)
    assert len(fake.prompts) == 1 and p["meta"]["source"] == "spark" and p["meta"]["repaired"]


def test_retry_feeds_errors_back_then_succeeds():
    broken = good()
    broken["text"][0]["region"] = "middle"  # schema error: not mechanical
    fake = Fake([broken, good()])
    p = run_plan(fake)
    assert len(fake.prompts) == 2 and p["meta"]["attempts"] == 2 and p["meta"]["source"] == "spark"
    assert "FAILED THESE CHECKS" in fake.prompts[1] and "region" in fake.prompts[1]


def test_falls_back_to_default_after_retries():
    broken = {"nonsense": True}
    fake = Fake([broken, RuntimeError("boom"), broken])
    p = run_plan(fake)
    assert len(fake.prompts) == 3  # 1 + 2 retries
    assert p["meta"]["source"] == "default"
    assert not list(VAL.iter_errors(p)) and codes(p) == set()
    assert next(t for t in p["text"] if t["role"] == "cta")["text"] == CONCEPT["cta"]


def test_model_cannot_overwrite_human_locked_text():
    p0 = good()
    for t in p0["text"]:
        t["text"] = "Model rewrite"
    p = run_plan(Fake([p0]))
    texts = {t["role"]: t["text"] for t in p["text"]}
    assert texts["cta"] == CONCEPT["cta"] and texts["hook"] == "Worried about fit?"


def test_look_preset_requested_is_enforced_or_defaulted():
    fake = Fake([good(), good(), good()])
    p = run_plan(fake, look="cutout_on_textile_plate")
    assert len(fake.prompts) == 3 and p["meta"]["source"] == "default"
    assert p["preset"] == "cutout_on_textile_plate"
