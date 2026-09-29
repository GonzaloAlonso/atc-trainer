"""The AI coach: explanations, grading, hints, levels, early detection, rewind, and the language coach."""

import time
from types import SimpleNamespace

import pytest

from atc import exercises
from atc.coach import grading
from atc.coach.llm import (CoachService, CoachUnavailable, ClaudeProvider, TemplateProvider, make_provider,
                           session_record)
from atc.coach.store import CoachStore
from atc.exercises.runner import ExerciseSandbox

from conftest import FakeNav

HOLDER = "human:alice"


def run_exercise(ex_id, policy, delay=0.0, level="advise", store=None):
    """Drive an exercise to its end in lockstep. policy: best (the reference answer as soon as the
    alert shows, or `delay` seconds later), early (the reference answer at early detection), none."""
    from atc import decisions
    box = ExerciseSandbox(FakeNav(), exercises.BY_ID[ex_id], "alice", coach_store=store, level=level)
    e = box.engine
    e.stop()
    time.sleep(0.05)
    while box.status == "running":
        e.step(5.0)
        if policy == "early":
            for en in list(e.coach.open.values()):
                if en["kind"] == "conflict" and en["dp"] is None and not en["actions"]:
                    a, b = (e.aircraft[i] for i in en["subjects"])
                    sit = en["situation"]["conflict"]
                    dp = decisions.conflict_decision(e, a, b, {"kind": "STCA", "t_to": sit["time_to_conflict_s"],
                                                               "h_nm": sit["predicted_h_nm"],
                                                               "v_ft": sit["predicted_v_ft"]}, e.t)
                    for ac_id, clr in next(o for o in dp.options if o["id"] == dp.best)["clearances"]:
                        e.issue(ac_id, [clr], HOLDER, actor=HOLDER)
        if policy in ("best", "early"):
            for dp in list(e.decisions.values()):
                if dp.status == "open" and HOLDER in dp.holders and dp.best and e.t - dp.created_t >= delay:
                    e.answer_decision(dp.id, {"action": dp.best}, HOLDER, actor=HOLDER)
    return box


# ---------------------------------------------------------------------------- grading validity
@pytest.mark.parametrize("ex_id", list(exercises.BY_ID))
def test_the_reference_answer_passes_every_drill_and_doing_nothing_fails(ex_id):
    good = run_exercise(ex_id, "best")
    assert good.status == "completed"
    assert good.summary["los"] == 0 and good.summary["passed"], good.summary
    assert good.summary["average"] >= 85                     # A

    bad = run_exercise(ex_id, "none")
    assert not bad.summary["passed"]
    assert bad.summary["average"] < 55
    if ex_id != "blocked-request":                            # a request nobody answers is safe, not a loss
        assert bad.summary["los"] >= 1 and bad.summary["letter"] == "E"


def test_acting_early_is_credited_and_acting_late_costs_points():
    early = run_exercise("head-on", "early")
    (e,) = early.engine.coach.entries_of("alice")
    assert e["early"] and e["actions"][0]["lead_s"] > 180 and e["grade"]["letter"] == "A"

    late = run_exercise("overtaking", "best", delay=60)
    (e,) = late.engine.coach.entries_of("alice")
    prompt = run_exercise("overtaking", "best")
    (p,) = prompt.engine.coach.entries_of("alice")
    assert e["grade"]["score"] < p["grade"]["score"]
    assert e["grade"]["parts"]["timeliness"] < p["grade"]["parts"]["timeliness"]


def test_feedback_names_the_better_option_when_separation_is_lost():
    box = run_exercise("head-on", "none")
    (e,) = box.engine.coach.entries_of("alice")
    fb = e["grade"]["feedback"]
    assert fb["key"] == "fb.no_action_los"
    assert "TRN" in fb["p"]["best"] and "monitor" not in fb["p"]["best"]


def test_grade_letters_and_clearance_costs():
    assert [grading.letter(s) for s in (100, 85, 84, 70, 55, 40, 39)] == ["A", "A", "B", "B", "C", "D", "E"]
    assert grading.clearance_cost("CLIMB", 360, None, 35000, 90) == 1.0
    assert grading.clearance_cost("DESCEND", 330, None, 35000, 90) == 1.5
    assert grading.clearance_cost("TURN", 30, "L", 35000, 90) == 2.0
    assert grading.clearance_cost("HEADING", 120, None, 35000, 90) == 2.0


# ---------------------------------------------------------------------------- explanations & levels
def until_decision(engine):
    """The shared test scenario's head-on pair meets ~10 min in; the alert fires 2 min before."""
    for _ in range(200):
        engine.step(5.0)
        if any(d.kind == "conflict" for d in engine.decisions.values()):
            return next(d for d in engine.decisions.values() if d.kind == "conflict")
    raise AssertionError("no conflict decision")


def test_every_option_is_explained_and_the_reference_is_marked(engine):
    engine.take_sector("RHN-H", HOLDER)
    until_decision(engine)
    dp = next(d for d in engine.decisions.values() if d.kind == "conflict")
    assert dp.best is not None
    whys = {o["id"]: o["why"] for o in dp.options}
    assert all(w and w["key"].startswith("why.") and w["text"] for w in whys.values())
    assert whys[dp.best]["key"] in ("why.best_h", "why.best_v")
    monitor = next(o for o in dp.options if o["role"] == "monitor")
    assert monitor["why"]["key"] == "why.monitor_bad"


def test_levels_hide_options_and_hints_reveal_the_answer(engine):
    engine.take_sector("RHN-H", HOLDER)
    engine.set_coach("alice", "evaluate")
    import json
    dp = until_decision(engine)
    frame = json.loads(engine.frame_for(HOLDER))
    fdp = next(d for d in frame["decisions"] if d["id"] == dp.id)
    assert fdp["questions"][0]["options"] == [] and fdp["hidden_options"] == len(dp.options)
    assert fdp["best"] is None

    engine.set_coach("alice", "hints")
    tiers = [engine.hint("alice", dp.id) for _ in range(3)]
    assert [h["tier"] for h in tiers] == [1, 2, 3]
    assert tiers[0]["message"]["key"] == "hint.c1"
    assert tiers[2]["message"]["key"] == "hint.solution" and tiers[2]["revealed"] == [dp.best]
    frame = json.loads(engine.frame_for(HOLDER))
    fdp = next(d for d in frame["decisions"] if d["id"] == dp.id)
    assert [o["id"] for o in fdp["questions"][0]["options"]] == [dp.best]
    assert frame["coach"]["open"][0]["tier"] == 3

    engine.set_coach("alice", "advise")
    frame = json.loads(engine.frame_for(HOLDER))
    fdp = next(d for d in frame["decisions"] if d["id"] == dp.id)
    assert len(fdp["questions"][0]["options"]) == len(dp.options) and fdp["best"] == dp.best
    assert engine.ai_pref("alice")["mode"] == "advisory"


def test_demonstrate_lets_the_ai_act_and_explain_without_grading(engine):
    engine.take_sector("RHN-H", HOLDER)
    engine.set_coach("alice", "demonstrate")
    assert engine.ai_pref("alice")["mode"] == "autonomous"
    until_decision(engine)
    for _ in range(60):
        engine.step(5.0)
        time.sleep(0.01)             # the agent answers on a worker thread
    narrations = [e for e in engine.events if e["kind"] == "coach" and e.get("msg", {}).get("key") == "narr.act"]
    assert narrations and narrations[0]["to"] == HOLDER
    graded = [e for e in engine.coach.closed if e["holder"] == HOLDER]
    assert all(e["grade"]["score"] is None for e in graded)


def test_the_old_ai_modes_map_onto_coach_levels(engine):
    engine.set_ai("alice", mode="advisory")
    assert engine.coach.level_of("alice") == "advise"
    engine.set_ai("alice", mode="autonomous")
    assert engine.coach.level_of("alice") == "demonstrate"
    engine.set_ai("alice", mode="off")
    assert engine.coach.level_of("alice") == "off"


# ---------------------------------------------------------------------------- probe, what-if, rewind
def test_probe_predicts_without_issuing(engine):
    engine.take_sector("RHN-H", HOLDER)
    until_decision(engine)
    ac = engine.find("TST001")
    before = (ac.assigned["alt"], list(ac.pending))
    from atc.clearances import Clearance
    out = engine.probe("TST001", [Clearance("CLIMB", 370)])
    assert (ac.assigned["alt"], ac.pending) == before              # nothing was issued
    assert out["baseline"]["los"] and not out["predicted"]["los"]
    track = out["tracks"][ac.id]
    assert len(track) > 20 and track[-1][2] > track[0][2] + 1500    # it climbs
    assert out["cpa"]["h"] is not None


def test_rewind_replays_the_same_traffic(engine):
    snap = engine.snapshot()
    for _ in range(12):
        engine.step(5.0)
    first = {k: (round(a.lat, 6), round(a.lon, 6), round(a.alt, 2)) for k, a in engine.aircraft.items()}
    engine.restore(snap)
    for _ in range(12):
        engine.step(5.0)
    again = {k: (round(a.lat, 6), round(a.lon, 6), round(a.alt, 2)) for k, a in engine.aircraft.items()}
    assert first == again


def test_exercise_rewind_starts_a_new_attempt(tmp_path):
    store = CoachStore(tmp_path / "coach.db")
    box = run_exercise("head-on", "none", store=store)
    first = box.engine.coach.sessions["alice"]
    (e,) = box.engine.coach.entries_of("alice")
    res = box.rewind(e["opened_t"] - 30)
    assert res["attempt"] == 2 and res["session"] != first
    assert box.status == "running" and not box.engine.paused
    assert store.session(res["session"])["parent"] == first
    assert store.session(first)["status"] == "completed"
    assert store.recording(first)                                   # the first attempt's replay is kept


# ---------------------------------------------------------------------------- language coach
class FakeClaude:
    def __init__(self, text="Good work. Keep scanning early.", stop_reason="end_turn", fail=None):
        self.calls = []
        self.text, self.stop_reason, self.fail = text, stop_reason, fail
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise self.fail
        return SimpleNamespace(stop_reason=self.stop_reason,
                               content=[SimpleNamespace(type="text", text=self.text)])


def _record():
    box = run_exercise("head-on", "best")
    s = {"context": "exercise", "exercise": "head-on", "summary": box.summary}
    return session_record(s, box.engine.coach.entries_of("alice"), "Head-on")


def test_claude_provider_request_shape():
    fake = FakeClaude()
    p = ClaudeProvider(client=fake)
    out = p.debrief(_record(), "de")
    assert out["text"].startswith("Good work")
    kw = fake.calls[0]
    assert kw["model"] == "claude-opus-5-5"
    assert kw["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in kw["betas"]
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert kw["output_config"]["effort"] in ("low", "medium", "high")
    grounding = kw["messages"][0]["content"][0]["text"]
    assert "TRN101" in grounding and "German" in grounding and "alice" not in grounding

    fake.text = "Because a descent was the smallest change."
    ans = p.answer("Why descend?", _record(), [("First question?", "First answer.")], "en")
    assert ans["text"].startswith("Because")
    msgs = fake.calls[-1]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]


def test_coach_falls_back_to_templates():
    svc = CoachService(ClaudeProvider(client=FakeClaude(stop_reason="refusal")))
    out = svc.debrief(_record(), "en")
    assert out["provider"] == "template" and out["messages"][0]["key"] == "db.verdict"
    assert "fallback_reason" in out
    ans = svc.answer("Why?", _record(), [], "es")
    assert ans["messages"][0]["key"] == "ask.unavailable"

    p = ClaudeProvider(client=FakeClaude(fail=CoachUnavailable("timeout")))
    assert CoachService(p).debrief(_record(), "en")["provider"] == "template"


def test_without_a_key_the_template_provider_is_used(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert isinstance(make_provider("claude"), TemplateProvider)
    assert isinstance(make_provider("template"), TemplateProvider)
