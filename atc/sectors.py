"""Sectorization: lateral regions stacked in vertical layers.

Twelve regions tile the core of the European traffic area without overlapping; each is split
vertically into Lower / Upper / High layers, giving 36 sectors. Airspace outside the regions is
unmanned. Boundaries are simplified approximations inspired by real area control centres, not
official airspace data.

A position belongs to at most one sector:  region polygon (lateral) × layer band (vertical),
with layer bands half-open [fl_min, fl_max).
"""

from .geo import point_in_polygon

# id, name, floor FL, ceiling FL
LAYERS = [
    ("L", "Lower", 0, 245),
    ("U", "Upper", 245, 345),
    ("H", "High", 345, 660),
]


def _box(lat0, lat1, lon0, lon1):
    return [(lat0, lon0), (lat1, lon0), (lat1, lon1), (lat0, lon1)]


# Regions share edges so they tile without gaps or overlaps (see tests/test_sectors.py).
REGIONS = [
    # north row, 50.0–55.5 N
    {"id": "LON", "name": "London", "poly": _box(50.0, 55.5, -6.0, 1.5)},
    {"id": "MAS", "name": "Maastricht", "poly": _box(50.0, 55.5, 1.5, 9.0)},
    {"id": "BER", "name": "Berlin", "poly": _box(50.0, 55.5, 9.0, 16.0)},
    # middle row, 46.0–50.0 N
    {"id": "BRE", "name": "Brest", "poly": _box(46.0, 50.0, -6.0, 1.0)},
    {"id": "PAR", "name": "Paris", "poly": _box(46.0, 50.0, 1.0, 6.0)},
    {"id": "RHN", "name": "Rhine", "poly": _box(46.0, 50.0, 6.0, 11.0)},
    {"id": "DAN", "name": "Danube", "poly": _box(46.0, 50.0, 11.0, 17.0)},
    # south row, 43.0–46.0 N
    {"id": "BOR", "name": "Bordeaux", "poly": _box(43.0, 46.0, -3.0, 3.0)},
    {"id": "ALP", "name": "Alps", "poly": _box(43.0, 46.0, 3.0, 10.0)},
    {"id": "PAD", "name": "Padova", "poly": _box(43.0, 46.0, 10.0, 16.0)},
    {"id": "BAL", "name": "Balkans", "poly": _box(42.0, 46.0, 16.0, 23.0)},
    # Iberia, 37.0–43.0 N
    {"id": "MAD", "name": "Madrid", "poly": _box(37.0, 43.0, -9.5, 3.0)},
]

for _r in REGIONS:
    lats = [p[0] for p in _r["poly"]]
    lons = [p[1] for p in _r["poly"]]
    _r["bbox"] = (min(lats), max(lats), min(lons), max(lons))

SECTORS = [
    {
        "id": "%s-%s" % (r["id"], lid), "name": "%s %s" % (r["name"], lname),
        "region": r["id"], "layer": lid, "fl_min": fmin, "fl_max": fmax, "poly": r["poly"],
    }
    for r in REGIONS for lid, lname, fmin, fmax in LAYERS
]
BY_ID = {s["id"]: s for s in SECTORS}
REGION_BY_ID = {r["id"]: r for r in REGIONS}


def region_at(lat, lon):
    for r in REGIONS:
        la0, la1, lo0, lo1 = r["bbox"]
        if la0 <= lat <= la1 and lo0 <= lon <= lo1 and point_in_polygon(lat, lon, r["poly"]):
            return r
    return None


def sector_at(lat, lon, alt_ft):
    """Sector id containing the position, or None for unmanned airspace."""
    r = region_at(lat, lon)
    if r is None:
        return None
    fl = alt_ft / 100.0
    for lid, _, fmin, fmax in LAYERS:
        if fmin <= fl < fmax:
            return "%s-%s" % (r["id"], lid)
    return None


def catalogue():
    """Static description for clients: regions, layers and sectors."""
    return {
        "layers": [{"id": l, "name": n, "fl_min": a, "fl_max": b} for l, n, a, b in LAYERS],
        "regions": [{"id": r["id"], "name": r["name"], "poly": r["poly"]} for r in REGIONS],
        "sectors": [{k: s[k] for k in ("id", "name", "region", "layer", "fl_min", "fl_max", "poly")}
                    for s in SECTORS],
    }
