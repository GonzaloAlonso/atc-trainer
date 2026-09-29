"""Decision drills: six short situations in Alps Upper (ALP-U, FL245–345), from a plain head-on to
a double conflict where the obvious level is taken. Each takes 6–9 minutes at 1× (speed up in
the sandbox while nothing is happening)."""

from ..training import fly
from . import Event, Exercise, Flight, toward

C = (44.5, 6.5)                   # the middle of ALP-U, where most situations meet
LEAD = fly(44.2, 4.6, 70, 12 * 3600, 1)   # 12 NM ahead of (44.2, 4.6) on track 070


def _background(tag):
    """Traffic above and below the practice layer, never in conflict: a realistic sky."""
    return [
        Flight("7e%s01" % tag, "BKG%s1" % tag, 45.4, 3.6, 37000, 460, 115),     # ALP-H
        Flight("7e%s02" % tag, "BKG%s2" % tag, 43.5, 9.4, 21000, 380, 290),     # ALP-L
        Flight("7e%s03" % tag, "BKG%s3" % tag, 45.6, 9.2, 39000, 470, 240),     # ALP-H
    ]


EXERCISES = [
    Exercise(
        id="head-on", order=1, level="easy", sector="ALP-U", duration_s=360,
        title="Head-on",
        brief="Two airliners fly towards each other at the same level. Spot it early and separate them.",
        goal="Keep at least 5 NM or 1000 ft between TRN101 and TRN202, acting well before the alert.",
        competencies=["situation_awareness", "separation", "decision"],
        flights=[toward("7c1011", "TRN101", C, 90, 450, 31000, 240),
                 toward("7c2022", "TRN202", C, 270, 450, 31000, 240, offset_nm=0.5)] + _background("1"),
    ),
    Exercise(
        id="crossing", order=2, level="easy", sector="ALP-U", duration_s=360,
        title="Crossing at right angles",
        brief="Two aircraft cross at 90° at the same level. Their paths meet over the middle of the sector.",
        goal="Separate TRN303 and TRN404 with one clearance.",
        competencies=["situation_awareness", "separation", "decision"],
        flights=[toward("7c3033", "TRN303", C, 0, 440, 33000, 250),
                 toward("7c4044", "TRN404", C, 90, 460, 33000, 250, offset_nm=1.0)] + _background("2"),
    ),
    Exercise(
        id="climb-through", order=3, level="medium", sector="ALP-U", duration_s=390,
        title="Climbing through traffic",
        brief="A departure climbs into your sector towards FL340 while traffic crosses at FL300.",
        goal="Keep TRN505's climb clear of TRN606; the smallest change is often to stop the climb.",
        competencies=["situation_awareness", "separation", "decision"],
        flights=[toward("7c5055", "TRN505", C, 80, 430, 25500, 270, vs_fpm=1000, target_ft=34000),
                 toward("7c6066", "TRN606", C, 260, 450, 30000, 270, offset_nm=1.0)] + _background("3"),
    ),
    Exercise(
        id="overtaking", order=4, level="medium", sector="ALP-U", duration_s=390,
        title="Catching up",
        brief="A fast jet follows a slower one on the same track and level. The gap is closing.",
        goal="Keep TRN707 at least 5 NM behind TRN808 or 1000 ft away from it.",
        competencies=["situation_awareness", "separation", "traffic_management"],
        focus=(44.35, 5.2, 650.0),
        flights=[Flight("7c8088", "TRN808", LEAD[0], LEAD[1], 32000, 390, 70),
                 Flight("7c7077", "TRN707", 44.2, 4.6, 32000, 480, 70)] + _background("4"),
    ),
    Exercise(
        id="blocked-request", order=5, level="medium", sector="ALP-U", duration_s=480,
        title="A request you can't grant yet",
        brief="TRN909 asks to climb to FL340, but traffic is about to cross its path at FL320.",
        goal="Keep everyone separated and give TRN909 its level as soon as it is safe.",
        competencies=["traffic_management", "decision", "communication"],
        focus=(44.75, 4.9, 650.0),
        flights=[Flight("7c9099", "TRN909", 44.8, 4.2, 30000, 430, 95),
                 toward("7c0100", "TRN010", (44.77, 4.70), 355, 440, 32000, 190)] + _background("5"),
        events=[Event(60, "request_level", "TRN909", 340)],
    ),
    Exercise(
        id="double-trouble", order=6, level="hard", sector="ALP-U", duration_s=540,
        title="The obvious level is taken",
        brief="A head-on pair at FL310, with traffic at FL320 right beside them; a second conflict follows.",
        goal="Resolve both conflicts without creating a new one: check the level is free first.",
        competencies=["situation_awareness", "separation", "decision", "workload"],
        flights=[toward("7c1111", "TRN111", C, 90, 450, 31000, 250),
                 toward("7c2222", "TRN222", C, 270, 450, 31000, 250, offset_nm=0.5),
                 toward("7c3333", "TRN333", C, 270, 450, 32000, 250, offset_nm=-3.0),
                 toward("7c4444", "TRN444", (44.1, 7.4), 0, 430, 29000, 420),
                 toward("7c5555", "TRN555", (44.1, 7.4), 90, 450, 29000, 420, offset_nm=0.8)]
        + _background("6"),
    ),
]
