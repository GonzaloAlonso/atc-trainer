"""ATC clearances: data model, ATC-shorthand parser and radiotelephony phrasing."""

import re
from dataclasses import dataclass, asdict
from typing import Optional

from .errors import Invalid

KINDS = ("CLIMB", "DESCEND", "HEADING", "TURN", "DIRECT", "SPEED", "RESUME")


@dataclass
class Clearance:
    kind: str                         # one of KINDS
    value: Optional[object] = None    # FL (int), heading/degrees (int), fix ident (str), IAS knots
    direction: Optional[str] = None   # "L"/"R" for HEADING (optional) and TURN (required)

    def to_dict(self):
        return asdict(self)

    def _int(self):
        try:
            return int(self.value)
        except (TypeError, ValueError):
            raise Invalid("value_invalid", kind=self.kind)

    def validate(self):
        k = self.kind
        if k not in KINDS:
            raise Invalid("clearance_kind", kind=k)
        if k in ("CLIMB", "DESCEND"):
            fl = self._int()
            if not 0 <= fl <= 600:
                raise Invalid("fl_range")
            self.value = fl
        elif k == "HEADING":
            self.value = self._int() % 360 or 360
            if self.direction not in (None, "L", "R"):
                raise Invalid("direction_invalid")
        elif k == "TURN":
            self.value = self._int()
            if not 1 <= self.value <= 180 or self.direction not in ("L", "R"):
                raise Invalid("turn_invalid")
        elif k == "DIRECT":
            if not self.value:
                raise Invalid("direct_fix")
            self.value = str(self.value).upper()
        elif k == "SPEED":
            self.value = self._int()
            if not 60 <= self.value <= 600:
                raise Invalid("speed_range")
        return self

    def phrase(self):
        k, v = self.kind, self.value
        if k == "CLIMB":
            return "climb flight level %03d" % v
        if k == "DESCEND":
            return "descend flight level %03d" % v
        if k == "HEADING":
            if self.direction:
                return "turn %s heading %03d" % ("left" if self.direction == "L" else "right", v)
            return "fly heading %03d" % v
        if k == "TURN":
            return "turn %s %d degrees" % ("left" if self.direction == "L" else "right", v)
        if k == "DIRECT":
            return "proceed direct %s" % v
        if k == "SPEED":
            return "speed %d knots" % v
        return "resume own navigation"


_NUM = r"(\d{1,3})"
_PATTERNS = [
    (re.compile(r"^(?:C|CL|CLB|CLIMB)$"), "CLIMB"),
    (re.compile(r"^(?:D|DES|DESC|DESCEND)$"), "DESCEND"),
    (re.compile(r"^(?:FL|M|MAINT|ALT)$"), "LEVEL"),
    (re.compile(r"^(?:TL|LT|LEFT)$"), "TL"),
    (re.compile(r"^(?:TR|RT|RIGHT)$"), "TR"),
    (re.compile(r"^(?:H|HDG|HEADING)$"), "HDG"),
    (re.compile(r"^(?:DCT|DIRECT|PD)$"), "DIRECT"),
    (re.compile(r"^(?:S|SPD|SPEED)$"), "SPEED"),
    (re.compile(r"^(?:RON|RESUME|OWN)$"), "RESUME"),
]


def parse_command(text):
    """Parse an ATC shorthand command.

    Returns (callsign, [Clearance]). Examples:
        "DLH4AB C 370"            climb FL370
        "DLH4AB D FL240 TL 270"   descend FL240, turn left heading 270
        "EZY12 TR 30D"            turn right 30 degrees
        "RYR1 DCT KPT S 280"      direct KPT, speed 280 kt
        "BAW9 FL 350"             climb or descend to FL350 (chosen automatically)
        "AFR7 RON"                resume own navigation
    LEVEL clearances come back as kind "LEVEL" and must be resolved against the current altitude.
    """
    tokens = text.strip().upper().split()
    if len(tokens) < 2:
        raise Invalid("cmd_syntax")
    callsign, rest = tokens[0], tokens[1:]
    out = []
    i = 0
    while i < len(rest):
        tok = rest[i]
        op = next((name for rx, name in _PATTERNS if rx.match(tok)), None)
        if op is None:
            raise Invalid("cmd_unknown", token=tok)
        i += 1
        if op == "RESUME":
            out.append(Clearance("RESUME"))
            continue
        if i >= len(rest):
            raise Invalid("cmd_value", token=tok)
        arg = rest[i]
        i += 1
        if arg == "FL" and op in ("CLIMB", "DESCEND") and i < len(rest):
            arg = rest[i]
            i += 1
        if op in ("CLIMB", "DESCEND", "LEVEL"):
            m = re.match(r"^(?:FL)?" + _NUM + "$", arg)
            if not m:
                raise Invalid("cmd_level", value=arg)
            out.append(Clearance(op, int(m.group(1))))
        elif op in ("TL", "TR", "HDG"):
            d = {"TL": "L", "TR": "R", "HDG": None}[op]
            rel = re.match(r"^\+?(\d{1,3})(?:D|DEG)$", arg)
            if rel:
                if d is None:
                    raise Invalid("cmd_relative")
                out.append(Clearance("TURN", int(rel.group(1)), d))
            elif re.match(r"^" + _NUM + "$", arg):
                out.append(Clearance("HEADING", int(arg), d))
            else:
                raise Invalid("cmd_heading", value=arg)
        elif op == "DIRECT":
            out.append(Clearance("DIRECT", arg))
        elif op == "SPEED":
            if not arg.isdigit():
                raise Invalid("cmd_speed", value=arg)
            out.append(Clearance("SPEED", int(arg)))
    return callsign, out


def readback(callsign, clearances):
    return ", ".join(c.phrase() for c in clearances) + ", " + callsign
