from atc.agents import RuleAgent


def advance(engine, seconds, dt=2.0):
    for _ in range(int(seconds / dt)):
        engine.step(dt)


def test_replay_spawns_recorded_traffic(engine):
    assert {a.callsign for a in engine.aircraft.values()} == {"TST001", "TST002", "TST003"}


def test_command_is_read_back(engine):
    res = engine.command("TST003 D 370 TL 20D")
    assert len(res["accepted"]) == 2 and not res["rejected"]
    texts = [e["text"] for e in engine.events]
    assert any("descend flight level 370, turn left 20 degrees" in t for t in texts)
    assert engine.find("TST003").controller == "human"


def test_sector_membership(engine):
    engine.set_sector("ALPS-UPPER")
    assert {engine.aircraft[i].callsign for i in engine._in_sector} >= {"TST001", "TST002"}


def test_conflict_decision_resolved_by_rule_agent(engine):
    engine.set_sector("ALPS-UPPER")
    dp = None
    for _ in range(450):              # the pair meets after ~590 s; STCA looks 120 s ahead
        advance(engine, 2)
        dp = next((d for d in engine.decisions.values() if d.kind == "conflict"), None)
        if dp:
            break
    assert dp is not None, "head-on pair should produce a decision point"
    d = dp.to_dict()
    q = d["questions"][0]
    assert q["type"] == "choice" and len(q["options"]) > 3
    assert all("los_duration_s" in o["predicted"] for o in q["options"])
    # doing nothing is predicted to lose separation in this scenario
    nothing = next(o for o in q["options"] if not o["clearances"])
    assert nothing["predicted"]["los"]

    answer = RuleAgent().decide(d)
    chosen = next(o for o in q["options"] if o["id"] == answer["action"])
    assert chosen["predicted"]["los_duration_s"] == 0
    engine.answer_decision(dp.id, answer, "ai:rules")
    assert engine.decisions[dp.id].status == "executed"

    # the executed clearance must actually keep the pair separated
    advance(engine, 300)
    assert engine.score["los"] == 0
    assert engine.score["decisions_ai"] == 1
