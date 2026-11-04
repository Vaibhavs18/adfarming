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
    assert roles["hook"]["text"] == "Worried about fit?" and roles["cta"]["text"] == "Shop Kurta"
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
    assert "cta_short" in codes(mod(lambda p: [t.update(text="See the kurta on our website today") for t in p["text"] if t["role"] == "cta"]))
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
    p["text"][1].update(text="Tampered cta - soft end card")
    fixed = cp.repair_plan(p, cp.lint_plan(p, CONCEPT, RULES, assets_info=ASSETS), CONCEPT, assets_info=ASSETS)
    assert codes(fixed) == set()
    assert not list(VAL.iter_errors(fixed))
    hook = next(t for t in fixed["text"] if t["role"] == "hook")
    assert 0 <= hook["start"] < hook["end"] <= 4.0  # clamped into beat 0
    assert fixed["background"][0]["src"] == "wall.png"  # people still swapped for the person-free one
    callout = next(t for t in fixed["text"] if t["role"] == "callout")
    assert len(callout["text"]) <= cp.REGION_CAP["left_mid"]
    assert next(t for t in fixed["text"] if t["role"] == "cta")["text"] == "Shop Kurta"
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
    c["cta_short"] = "If you are new to sarees and kurtas, do come and see everything we have here"
    p = cp.default_plan(c, None)
    p = cp.repair_plan(p, [], c)
    cta = next(t for t in p["text"] if t["role"] == "cta")
    assert cta["text"] == c["cta_short"] and cta["size"] < 64


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
    assert next(t for t in p["text"] if t["role"] == "cta")["text"] == "Shop Kurta"


def test_model_cannot_overwrite_human_locked_text():
    p0 = good()
    for t in p0["text"]:
        t["text"] = "Model rewrite"
    p = run_plan(Fake([p0]))
    texts = {t["role"]: t["text"] for t in p["text"]}
    assert texts["hook"] == "Worried about fit?" and cp.cta_issue(texts["cta"]) is None
    p1 = run_plan(Fake([p0]), brand_colours=None)
    assert p1["meta"]["source"] == "spark"
    c2 = dict(CONCEPT, cta_short="Shop Kurta now")  # a human-edited short CTA always wins over the model
    p0["text"][1]["text"] = "Something else"
    q = cp.repair_plan(p0, [], c2, assets_info=ASSETS)
    assert next(t for t in q["text"] if t["role"] == "cta")["text"] == "Shop Kurta now"


def test_look_preset_requested_is_enforced_or_defaulted():
    fake = Fake([good(), good(), good()])
    p = run_plan(fake, look="cutout_on_textile_plate")
    assert len(fake.prompts) == 3 and p["meta"]["source"] == "default"
    assert p["preset"] == "cutout_on_textile_plate"


# ----------------------------------------------------------------- composed-look fixes (F2)
INTERNAL_CONCEPT = dict(CONCEPT, cta="If you're new to sarees, see this Maheshwari on our website - soft end card")
HEAD = {"top_frac": 0.29, "bbox_frac": [0.2, 0.15, 0.8, 1.0]}


def test_cta_short_never_carries_internal_direction():
    assert cp.cta_issue("See this Maheshwari on our website - soft end card")
    assert cp.cta_issue("Shop Maheshwari now please sir") and cp.cta_issue("soft end card") and cp.cta_issue("")
    assert cp.cta_issue("Shop Maheshwari") is None
    assert cp.derive_cta_short(INTERNAL_CONCEPT) == "Shop Maheshwari"
    p = cp.default_plan(INTERNAL_CONCEPT, None)
    cta = next(t for t in p["text"] if t["role"] == "cta")
    assert cta["text"] == "Shop Maheshwari" == cta["cta_short"]
    assert not any("soft" in t["text"] or " - " in t["text"] for t in p["text"])
    assert not list(VAL.iter_errors(p))
    # a model that echoes the direction is repaired to the short CTA, no retry needed
    bad = copy.deepcopy(p)
    next(t for t in bad["text"] if t["role"] == "cta").update(text=INTERNAL_CONCEPT["cta"], cta_short=INTERNAL_CONCEPT["cta"][:40])
    probs = cp.lint_plan(bad, INTERNAL_CONCEPT, RULES)
    assert any(x["code"] in ("cta_short", "schema") for x in probs)
    fixed = cp.repair_plan(bad, [], INTERNAL_CONCEPT)
    assert next(t for t in fixed["text"] if t["role"] == "cta")["text"] == "Shop Maheshwari"
    assert not [x for x in cp.lint_plan(fixed, INTERNAL_CONCEPT, RULES) if x["level"] == "error"]


def test_human_cta_short_wins_and_prompt_hides_internal_direction():
    c = dict(INTERNAL_CONCEPT, cta_short="Shop the saree")
    p = cp.repair_plan(cp.default_plan(c, None), [], c)
    assert next(t for t in p["text"] if t["role"] == "cta")["text"] == "Shop the saree"
    p["text"][-1]["text"] = p["text"][-1]["cta_short"] = "Something else"
    assert "locked" in {x["code"] for x in cp.lint_plan(p, c, RULES)}
    prompt = cp._build_prompt(c, "b", "", ASSETS, "", None, [])
    assert "Shop the saree" in prompt and "soft end card" not in prompt
    free = cp._build_prompt(INTERNAL_CONCEPT, "b", "", ASSETS, "", None, [])
    assert "NEVER show it" in free and "Shop Maheshwari" in free


def test_text_capped_at_80_chars():
    c = copy.deepcopy(CONCEPT)
    c["beats"][0]["on_screen_text"] = "word " * 30
    assert len(cp._hook_text(c)) <= cp.MAX_TEXT_CHARS
    p = cp.default_plan(c, None)
    assert not list(VAL.iter_errors(p))
    q = good()
    q["text"][0]["text"] = "x" * 100
    assert len(cp.repair_plan(q, [], CONCEPT)["text"][0]["text"]) <= cp.MAX_TEXT_CHARS
    assert len(cp.clip_text("a" * 200)) <= cp.MAX_TEXT_CHARS


def test_headline_stays_above_head_top():
    c = copy.deepcopy(CONCEPT)
    c["beats"][0]["on_screen_text"] = "Maheshwari saree that feels comfortable to wear"
    p = cp.default_plan(c, None, head=HEAD)
    hook = next(t for t in p["text"] if t["role"] == "hook")
    top_px = int(HEAD["top_frac"] * 1920)
    assert cp.HEADLINE_TOP_PX + cp.headline_height_px(hook["text"], hook["size"], 8) <= top_px - cp.HEADLINE_GAP_PX
    assert hook["size"] < 96 and hook["stroke"][0] == 8 and hook["weight"] == "black"
    assert hook["keyword"] and hook["keyword"].lower() in hook["text"].lower()
    # an oversized headline from the model is flagged then fixed
    big = copy.deepcopy(p)
    big["text"][0]["size"] = 150
    assert "headline_room" in {x["code"] for x in cp.lint_plan(big, c, RULES)}
    assert not [x for x in cp.lint_plan(cp.repair_plan(big, [], c), c, RULES) if x["code"] == "headline_room" and x["level"] == "error"]
    # without a head box nothing is touched
    q = cp.repair_plan(good(), [], CONCEPT)
    assert next(t for t in q["text"] if t["role"] == "hook")["size"] == 96


def test_callout_goes_to_side_margin_away_from_head():
    p = good()
    p["meta"]["head"] = {"top_frac": 0.29, "bbox_frac": [0.05, 0.15, 0.65, 1.0]}  # speaker off to the left
    p["text"].append(dict(p["text"][0], id="c1", role="callout", region="left_mid", style="callout_tag", text="Soft Maheshwari",
                          start=4.5, end=6.5, beat=1, plate="#fbbf24", fill="#111111", animation="slide_left"))
    side, ov = cp.callout_side(p)
    assert side == "right_mid" and ov["left_mid"] > 0.02 >= ov["right_mid"]
    assert "callout_head" in codes(p)
    fixed = cp.repair_plan(p, [], CONCEPT, assets_info=ASSETS)
    co = next(t for t in fixed["text"] if t["role"] == "callout")
    assert co["region"] == "right_mid" and co["point"] == "shoulder"
    assert "callout_head" not in codes(fixed)


def test_brand_colour_has_two_beats_contrast_and_rim():
    p = cp.default_plan(CONCEPT, ["#5a1230", "#a3321f"])
    assert p["preset"] == "cutout_on_brand_colour" and len(p["background"]) >= 2
    l0, l1 = (cp._bg_luma(b) for b in p["background"][:2])
    assert l1 > l0 + 0.05 and cp._lum_contrast(l1, cp.SUBJECT_LUMA) >= cp.MIN_BG_CONTRAST
    assert p["subject"]["rim"]["enabled"] and p["subject"]["rim"]["px"] >= 2  # dark first beat -> rim light
    flat = copy.deepcopy(p)
    flat["background"] = flat["background"][:1]
    assert "bg_beats" in codes(flat)
    dark_norim = copy.deepcopy(p)
    dark_norim["subject"]["rim"]["enabled"] = False
    assert "rim_light" in codes(dark_norim)
    fixed = cp.repair_plan(flat, [], CONCEPT)
    assert len(fixed["background"]) >= 2 and codes(fixed) == set() and fixed["subject"]["rim"]["enabled"]
    # all-dark plan against a dark subject is lightened, not left flat
    low = copy.deepcopy(p)
    low["background"] = [dict(low["background"][0], colors=["#3a1020", "#2a0a1a"])]
    low["preset"] = "cutout_on_brand_colour"
    assert "bg_contrast" in codes(low)
    assert cp._lum_contrast(max(cp._bg_luma(b) for b in cp.repair_plan(low, [], CONCEPT)["background"]), cp.SUBJECT_LUMA) >= cp.MIN_BG_CONTRAST


def test_background_stills_need_explicit_no_people_flag():
    assets = {"backdrops": [{"name": "unknown.png"}, {"name": "ok.png", "has_people": False},
                            {"name": "desc.png", "has_people": False, "description": "a woman by the window"}]}
    assert cp._asset_names(assets, "backdrops", people_ok=False) == {"ok.png"}
    p = good()
    p["background"][0].update(kind="blur_still", src="unknown.png")
    assert "backdrop" in {x["code"] for x in cp.lint_plan(p, CONCEPT, RULES, assets_info=assets) if x["level"] == "error"}


def test_beat_transitions_and_subject_scale():
    p = good()
    segs = p["subject"]["segments"]
    assert "transition_in" not in segs[0]
    assert all(s["transition_in"]["type"] in ("push", "flash", "dissolve") for s in segs[1:])
    assert segs[1]["transition_in"] == {"type": "push", "dur": 0.25}
    assert segs[-1]["transition_in"] == {"type": "flash", "dur": 0.15}
    assert all(s["subject_scale"] == 1.0 for s in segs)
    # head sizes differ per clip -> scale normalised to the median head, clamped
    q = copy.deepcopy(p)
    q["meta"]["head"] = {"top_frac": 0.29, "per_clip": {"a.mov": 0.20, "b.mov": 0.25}}
    r = cp.repair_plan(q, [], CONCEPT)
    sc = {s["clip"]: s["subject_scale"] for s in r["subject"]["segments"]}
    assert sc["a.mov"] > 1.0 > sc["b.mov"] and all(0.85 <= v <= 1.15 for v in sc.values())
    assert not list(VAL.iter_errors(r))
    # a bad transition name is coerced, an old plan without the new keys still validates
    q = copy.deepcopy(p)
    q["subject"]["segments"][1]["transition_in"] = {"type": "wipe", "dur": 9}
    assert cp.repair_plan(q, [], CONCEPT)["subject"]["segments"][1]["transition_in"] == {"type": "push", "dur": 0.6}
    old = copy.deepcopy(p)
    for s in old["subject"]["segments"]:
        s.pop("transition_in", None), s.pop("subject_scale", None)
    old["subject"].pop("rim")
    assert not [x for x in cp.lint_plan(old, CONCEPT, RULES) if x["code"] == "schema"]


def test_caption_chunks_3_to_6_words_and_08_to_20_s():
    long = [{"text": "Maine pehli baar saree pehni aur mujhe bilkul bhi mushkil nahi hui kyunki ye bahut soft hai", "start": 0.0, "end": 6.0}]
    out = cp.rechunk_captions(long)
    assert out and all(3 <= len(c["text"].split()) <= 6 for c in out)
    assert all(0.8 - 0.011 <= c["end"] - c["start"] <= 2.0 + 0.011 for c in out)
    assert " ".join(c["text"] for c in out) == long[0]["text"]
    p = good()
    p["captions"].update(source="plan", chunks=[{"text": "ok", "start": 0.0, "end": 0.4}, {"text": "one two three four five six seven eight", "start": 0.4, "end": 8.0}])
    assert "chunk_size" in codes(p)
    fixed = cp.repair_plan(p, [], CONCEPT)
    assert "chunk_size" not in codes(fixed) and codes(fixed) == set()
    assert fixed["captions"]["chunking"] == {"words": [3, 6], "dur": [0.8, 2.0]}
    # impossible timing (6 words in 0.3 s followed by speech) degrades to speech-timed captions, never blocks
    q = good()
    q["captions"].update(source="plan", chunks=[{"text": "a b c d e f", "start": 0.0, "end": 0.3}, {"text": "g h i j k l", "start": 0.3, "end": 0.5}])
    r = cp.repair_plan(q, [], CONCEPT)
    assert r["captions"]["source"] == "asr" and r["captions"]["chunks"] == [] and codes(r) == set()


def test_prompt_lists_enum_values_literally_and_new_rules():
    prompt = cp._build_prompt(CONCEPT, "b", "", ASSETS, "", None, [], head=HEAD)
    for v in ("cutout_on_brand_colour", "pill_headline", "top_center", "slide_right", "dissolve", "flash", "devanagari", "shoulder", "black"):
        assert json.dumps(v) in prompt
    assert "at most 4 words" in prompt and "SPEAKER GEOMETRY" in prompt and "3-6 words" in prompt
    # every enum in the table is real: it matches the schema
    assert "- fps: 24 | 25 | 30" in prompt


def test_plan_composition_passes_head_and_survives_invalid_enum():
    bad = good()
    bad["text"][0]["animation"] = "zoom_in"
    bad["text"][0]["font"] = "comic"
    bad["subject"]["segments"][1]["transition_in"] = {"type": "wipe", "dur": 0.2}
    fake = Fake([bad])
    p = run_plan(fake, head=HEAD)
    assert len(fake.prompts) == 1 and "SPEAKER GEOMETRY" in fake.prompts[0]
    assert p["meta"]["source"] == "spark" and p["meta"]["head"] == HEAD and p["meta"]["attempts"] == 1
    assert not list(VAL.iter_errors(p)) and codes(p) == set()


def test_default_plan_for_real_style_concept_is_clean():
    c = dict(INTERNAL_CONCEPT, title="Beginner Ho Toh Bhi Comfortable")
    c["beats"] = [{"clip": "IMG_1.mov", "src_in": 9.2, "src_out": 13.84, "on_screen_text": "Beginner with draping?"},
                  {"clip": "IMG_2.MOV", "src_in": 5.1, "src_out": 8.6, "on_screen_text": "not comfortable in sarees?"},
                  {"clip": "IMG_1.mov", "src_in": 15.5, "src_out": 18.5, "on_screen_text": "Maheshwari felt very comfortable"},
                  {"clip": "IMG_2.MOV", "src_in": 10.5, "src_out": 13.5, "on_screen_text": "If you're new to sarees"}]
    p = cp.default_plan(c, ["#5a1230", "#a3321f"], head=HEAD)
    assert not list(VAL.iter_errors(p))
    assert [x for x in cp.lint_plan(p, c, RULES) if x["level"] == "error"] == []
    assert len(p["background"]) >= 2 and next(t for t in p["text"] if t["role"] == "cta")["text"] == "Shop Maheshwari"


def test_keyword_is_a_bare_word_of_the_hook():
    p = good()
    next(t for t in p["text"] if t["role"] == "hook")["keyword"] = "fit?"
    assert next(t for t in cp.repair_plan(p, [], CONCEPT)["text"] if t["role"] == "hook")["keyword"] == "fit"
