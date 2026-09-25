from types import SimpleNamespace

from atc import conflicts


def ac(id, lat, lon, alt, hdg, gs=450, vs=0):
    return SimpleNamespace(id=id, lat=lat, lon=lon, alt=alt, hdg=hdg, tas=gs, vs=vs,
                           vert_mode="PLAN", cleared_alt=None)


def test_head_on_same_level_is_predicted():
    a = ac("a", 47.0, 8.0, 35000, 90)
    b = ac("b", 47.0, 8.5, 35000, 270)        # ~20 NM apart, 900 kt closure
    found = conflicts.detect([a, b])
    assert len(found) == 1 and found[0]["kind"] == "STCA" and 0 < found[0]["t_to"] <= 120


def test_vertically_separated_is_clear():
    a = ac("a", 47.0, 8.0, 35000, 90)
    b = ac("b", 47.0, 8.5, 36000, 270)
    assert conflicts.detect([a, b]) == []


def test_current_loss_of_separation():
    a = ac("a", 47.0, 8.0, 35000, 90)
    b = ac("b", 47.0, 8.05, 35300, 90)        # 2 NM in trail
    found = conflicts.detect([a, b])
    assert found and found[0]["kind"] == "LOS"


def test_tma_minimum_is_3nm():
    a = ac("a", 47.0, 8.0, 8000, 90, gs=250)
    b = ac("b", 47.0, 8.06, 8000, 90, gs=250)  # ~2.5 NM in trail, same speed
    c = ac("c", 47.0, 8.1, 8000, 90, gs=250)   # ~4 NM from a: fine below FL100
    kinds = {(x["a"], x["b"]) for x in conflicts.detect([a, b, c])}
    assert ("a", "b") in kinds and ("a", "c") not in kinds


def test_climb_capped_by_cleared_level():
    a = ac("a", 47.0, 8.0, 33000, 90, vs=2000)
    a.vert_mode, a.cleared_alt = "ALT", 34000
    b = ac("b", 47.0, 8.5, 36000, 270)
    assert conflicts.detect([a, b]) == []
