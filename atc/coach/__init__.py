"""The AI coach: explains decision options, grades what the trainee did, gives hints and
debriefs.

- explain.py  why each option of a decision point is (or isn't) the answer
- grading.py  scores one decision: safety, timeliness, efficiency, compared with the AI's answer
- journal.py  the per-engine Coach: records decisions as they happen, hints, narration, levels
- store.py    sessions, graded entries, recordings and chats (data/coach.db)
- llm.py      natural-language debriefs and answers: Claude, with a template fallback
- text.py     English templates for every coach message (the UI renders the same keys in EN/DE/ES)
"""

LEVELS = ("off", "evaluate", "hints", "advise", "demonstrate")
DEFAULT_LEVEL = "hints"
