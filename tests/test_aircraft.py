from atc.aircraft import Aircraft, FlightPlan
from atc.clearances import Clearance
from atc.geo import angle_diff, distance_nm

from conftest import FakeNav, fly

T0 = 1_000_000


def straight_plan(alt=35000, gs=450, trk=90, n=30, every=60, lat=47.0, lon=5.0):
    plan = FlightPlan("abc123")
    for k in range(n):
        la, lo = fly(lat, lon, trk, gs, k * every)
        plan.add((T0 + k * every, "abc123", "TST9", "Testland", la, lo, alt, gs, trk, 0.0, 4, "1000"))
    return plan


def run(ac, seconds, dt=2.0, t=T0):
    steps = int(seconds / dt)
    for i in range(steps):
        ac.step(t + i * dt, dt)
    return t + steps * dt


def test_follows_recording():
    plan = straight_plan()
    ac = Aircraft(plan, T0)
    run(ac, 600)
    lat, lon, *_ = plan.state_at(T0 + 600)
    assert distance_nm(ac.lat, ac.lon, lat, lon) < 1.0
    assert abs(ac.alt - 35000) < 50


def test_climb_respects_performance_and_captures_level():
    ac = Aircraft(straight_plan(), T0)
    ok, _ = ac.issue(Clearance("CLIMB", 390), T0, FakeNav(), "human")
    assert ok and ac.assigned["alt"] == 39000
    run(ac, 60)
    assert ac.alt < 35000 + 1100          # ~1000 fpm above FL250 after pilot delay
    run(ac, 400, t=T0 + 60)
    assert abs(ac.alt - 39000) < 30 and abs(ac.vs) < 100


def test_unable_above_ceiling_and_wrong_direction():
    ac = Aircraft(straight_plan(), T0)
    ok, reply = ac.issue(Clearance("CLIMB", 450), T0, FakeNav(), "human")
    assert not ok and "unable" in reply
    ok, reply = ac.issue(Clearance("CLIMB", 300), T0, FakeNav(), "human")
    assert not ok and "above" in reply


def test_turn_left_goes_the_long_way_when_told():
    ac = Aircraft(straight_plan(trk=90), T0)
    ac.issue(Clearance("HEADING", 120, "L"), T0, FakeNav(), "human")   # 330° turn to the left
    headings = []
    t = T0
    for _ in range(120):
        ac.step(t, 2.0)
        t += 2.0
        headings.append(ac.hdg)
    assert any(270 <= h <= 330 for h in headings), "should pass through west"
    run(ac, 400, t=t)
    assert abs(angle_diff(120, ac.hdg)) < 1


def test_direct_to_fix_then_rejoin_route():
    nav = FakeNav()
    ac = Aircraft(straight_plan(lat=47.4, lon=7.0), T0)
    ac.issue(Clearance("DIRECT", "ALPHA"), T0, nav, "human")
    t = run(ac, 30)
    assert ac.lat_mode == "DCT"
    fix = nav.find("ALPHA")
    d0 = distance_nm(ac.lat, ac.lon, fix["lat"], fix["lon"])
    run(ac, 60, t=t)
    assert distance_nm(ac.lat, ac.lon, fix["lat"], fix["lon"]) < d0
    run(ac, 1200, t=t + 60)
    assert ac.lat_mode == "PLAN" and ac.diverged


def test_pilot_delay():
    ac = Aircraft(straight_plan(), T0)
    ac.issue(Clearance("DESCEND", 300), T0, FakeNav(), "human")
    ac.step(T0, 1.0)
    assert ac.vert_mode == "PLAN"         # not yet: pilots take 3-8 s
    run(ac, 10, dt=1.0, t=T0 + 1)
    assert ac.vert_mode == "ALT" and ac.vs < 0
