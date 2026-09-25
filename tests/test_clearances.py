import pytest

from atc.clearances import Clearance, parse_command, readback


@pytest.mark.parametrize("text,expected", [
    ("DLH4AB C 370", [("CLIMB", 370, None)]),
    ("dlh4ab d fl 240 tl 270", [("DESCEND", 240, None), ("HEADING", 270, "L")]),
    ("EZY12 TR 30D S 250", [("TURN", 30, "R"), ("SPEED", 250, None)]),
    ("RYR1 DCT KPT", [("DIRECT", "KPT", None)]),
    ("AFR7 RON", [("RESUME", None, None)]),
    ("BAW9 FL 350", [("LEVEL", 350, None)]),
    ("KLM1 H 090", [("HEADING", 90, None)]),
])
def test_parse(text, expected):
    cs, clrs = parse_command(text)
    assert cs == text.split()[0].upper()
    assert [(c.kind, c.value, c.direction) for c in clrs] == expected


@pytest.mark.parametrize("text", ["X", "X ZZ 1", "X C", "X C ABC", "X H 30D", "X S FAST"])
def test_parse_errors(text):
    with pytest.raises(ValueError):
        parse_command(text)


def test_validate_and_phrase():
    assert Clearance("HEADING", 0).validate().value == 360
    with pytest.raises(ValueError):
        Clearance("CLIMB", 900).validate()
    with pytest.raises(ValueError):
        Clearance("TURN", 30).validate()   # direction required
    text = readback("TST1", [Clearance("DESCEND", 240), Clearance("HEADING", 270, "L")])
    assert text == "descend flight level 240, turn left heading 270, TST1"
