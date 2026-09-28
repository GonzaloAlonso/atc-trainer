import pytest

from atc import sectors
from atc.control import Control, ControlError, display, human
from atc.geo import point_in_polygon


# ---------------------------------------------------------------------------- catalogue
def test_catalogue_shape():
    assert len(sectors.SECTORS) == len(sectors.REGIONS) * len(sectors.LAYERS) == 36
    assert len(sectors.BY_ID) == 36
    layers = sectors.LAYERS
    assert layers[0][2] == 0 and layers[-1][3] >= 600
    for (_, _, _, top), (_, _, bottom, _) in zip(layers, layers[1:]):
        assert top == bottom, "vertical layers must be contiguous"


def test_regions_never_overlap():
    for i in range(0, 211):
        lat = 36.0 + i * 0.1
        for j in range(0, 341):
            lon = -10.0 + j * 0.1
            inside = [r["id"] for r in sectors.REGIONS if point_in_polygon(lat, lon, r["poly"])]
            assert len(inside) <= 1, (lat, lon, inside)


@pytest.mark.parametrize("lat,lon,alt,expected", [
    (51.5, -0.5, 36000, "LON-H"),
    (47.0, 8.0, 30000, "RHN-U"),
    (44.5, 6.0, 20000, "ALP-L"),
    (44.5, 6.0, 24500, "ALP-U"),      # FL245 belongs to the upper layer
    (44.5, 6.0, 24499, "ALP-L"),
    (44.5, 6.0, 34500, "ALP-H"),
    (40.4, -3.7, 38000, "MAD-H"),
    (60.0, 10.0, 35000, None),        # outside the sectorization: unmanned
])
def test_sector_at(lat, lon, alt, expected):
    assert sectors.sector_at(lat, lon, alt) == expected


# ---------------------------------------------------------------------------- control rules
class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def test_take_release_and_bandboxing():
    c = Control()
    alice, bob = human("alice"), human("bob")
    assert c.take("ALP-U", alice) is None
    c.take("ALP-H", alice)                               # several sectors per user
    assert c.sectors_of(alice) == ["ALP-H", "ALP-U"]
    with pytest.raises(ControlError) as e:
        c.take("ALP-U", bob)
    assert e.value.status == 409 and "alice" in str(e.value)
    with pytest.raises(ControlError) as e:
        c.release("ALP-U", bob)
    assert e.value.status == 403
    assert c.take("ALP-U", bob, force=True) == alice     # admin override
    c.release("ALP-H", alice)
    assert c.sectors_of(alice) == [] and c.holder("ALP-U") == bob
    with pytest.raises(ControlError) as e:
        c.take("NOPE-U", bob)
    assert e.value.status == 404


def test_ai_holding_and_takeover():
    c = Control()
    alice, bob = human("alice"), human("bob")
    c.assign_ai("PAR-U", "rules", alice)                  # a free sector
    assert c.holder("PAR-U") == "ai:rules" and display("ai:rules") == "AI (rules)"
    c.take("PAR-U", bob)                                  # humans may take over from the AI
    assert c.holder("PAR-U") == bob
    with pytest.raises(ControlError):
        c.assign_ai("PAR-U", "rules", alice)             # not alice's to hand over
    c.assign_ai("PAR-U", "rules", bob)                   # but bob may hand his own
    assert c.holder("PAR-U") == "ai:rules"


def test_idle_controllers_are_released():
    clock = Clock()
    c = Control(idle_s=600, clock=clock)
    c.take("BER-U", human("alice"))
    c.assign_ai("BER-H", "rules", human("alice"))
    clock.t += 300
    c.seen("alice")
    clock.t += 500
    assert c.reap_idle() == []
    clock.t += 200
    assert c.reap_idle() == [("BER-U", "human:alice")]
    assert c.holder("BER-H") == "ai:rules", "AI-held sectors are never released for inactivity"
