"""Every interface string exists in English, German and Spanish, with the same placeholders, and
every coach message the server can send has a translation."""

import json
import re
from pathlib import Path

from atc.coach.text import T

LOCALES = Path(__file__).resolve().parent.parent / "public" / "locales"
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def flat(d, prefix=""):
    out = {}
    for k, v in d.items():
        key = prefix + k
        if isinstance(v, dict) and prefix + k != "coach":
            out.update(flat(v, key + "."))
        elif isinstance(v, dict):
            out.update({key + ":" + ck: cv for ck, cv in v.items()})
        else:
            out[key] = v
    return out


def load(lang):
    return flat(json.loads((LOCALES / ("%s.json" % lang)).read_text(encoding="utf-8")))


def test_all_languages_have_the_same_keys_and_placeholders():
    en = load("en")
    for lang in ("de", "es"):
        other = load(lang)
        assert set(other) == set(en), (lang, sorted(set(en) ^ set(other))[:20])
        for key, text in en.items():
            assert set(PLACEHOLDER.findall(text)) == set(PLACEHOLDER.findall(other[key])), (lang, key)
            assert other[key].strip(), (lang, key)


def test_every_server_coach_message_is_translated():
    en = load("en")
    for key, text in T.items():
        assert en.get("coach:" + key) == text, key       # the English file mirrors the server's texts


def test_every_exercise_has_texts():
    from atc import exercises
    en = load("en")
    for ex_id in exercises.BY_ID:
        for part in ("title", "brief", "goal"):
            assert en.get("ex.%s.%s" % (ex_id, part)), (ex_id, part)


def test_every_server_error_is_translated():
    from atc.errors import EN
    en = load("en")
    for code, text in EN.items():
        assert en.get("errors." + code) == text, code      # the English file mirrors the server's texts
    assert not {k for k in en if k.startswith("errors.")} - {"errors." + c for c in EN}
