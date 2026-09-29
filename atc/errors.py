"""Errors a user can see, in their language.

Raise a UserError (or Invalid / Denied / Busy, which are also ValueError / PermissionError /
OverflowError, so existing handling keeps working) with a code and parameters. str(exc) is the
English text, for logs, tests and agents; the API answers
{"detail": <the text in the caller's language>, "code": ..., "params": {...}}.

The English texts are here. German and Spanish (and the English copy the interface uses) live in
public/locales/<lang>.json under "errors"; tests/test_locales.py keeps them in step.
Pilots' replies ("unable flight level 450") are radio phraseology and stay in ICAO English.
"""

import json

from . import config

LANGS = ("en", "de", "es")

EN = {
    # access
    "cross_origin": "cross-origin request refused",
    "auth_required": "authentication required",
    "password_change_required": "password change required",
    "admin_only": "admin only",
    "login_throttled": "too many failed attempts, try again in {s} s",
    "login_invalid": "invalid username or password",
    "invalid_request": "invalid request: check {fields}",
    "page_outdated": "This page is outdated (ATC Trainer {version} is running). "
                     "Reload it with Ctrl+Shift+R (Cmd+Shift+R on a Mac).",
    # accounts
    "username_format": "username must be 3-32 characters: letters, digits, . _ -",
    "password_short": "password must be at least {n} characters",
    "password_long": "password is too long",
    "role_invalid": "role must be one of: {roles}",
    "username_taken": "username '{user}' is already taken",
    "lang_invalid": "language must be one of: {langs}",
    "coach_level_invalid": "coach level must be one of: {levels}",
    "tutorial_state_invalid": "invalid tutorial state",
    "no_such_user": "no such user",
    "last_admin": "there must be at least one active admin",
    "own_role": "you can't disable yourself or change your own role",
    "own_delete": "you can't delete your own account",
    "password_wrong": "current password is incorrect",
    "password_same": "choose a different password",
    # sandboxes, tutorial and exercises
    "no_tutorial_session": "no training session: start the tutorial first",
    "no_exercise_session": "no exercise session: start the exercise first",
    "too_many_sandboxes": "too many training sessions, try again later",
    "no_such_exercise": "no such exercise",
    "no_such_situation": "no such situation",
    "rewind_input": "give to_t or entry",
    "nothing_to_rewind": "nothing to rewind to yet",
    # simulation and sectors
    "sim_admin_only": "only admins can control the shared simulation",
    "lockstep_first": "enable lockstep first",
    "sandbox_reset": "restart the training scenario with POST /api/tutorial/start",
    "no_data": "no recorded data yet",
    "unknown_sector": "unknown sector {sector}",
    "sector_held": "{sector} is controlled by {holder}",
    "ai_mode_invalid": "mode must be one of {modes}",
    "unknown_agent": "unknown agent '{name}' (available: {available})",
    # aircraft, clearances and decisions
    "no_such_aircraft": "no such aircraft",
    "no_aircraft": "no aircraft {ident}",
    "not_your_aircraft": "{callsign} is in {sector}, controlled by {holder}",
    "unknown_fix": "unknown fix {fix}",
    "probe_input": "give an aircraft and clearances, or a command",
    "clearance_kind": "unknown clearance kind '{kind}'",
    "value_invalid": "invalid value for {kind}",
    "fl_range": "flight level out of range",
    "direction_invalid": "direction must be L or R",
    "turn_invalid": "TURN needs 1-180 degrees and direction L/R",
    "direct_fix": "DIRECT needs a fix identifier",
    "speed_range": "speed out of range",
    "cmd_syntax": "expected: CALLSIGN COMMAND [VALUE] ...",
    "cmd_unknown": "unknown command '{token}'",
    "cmd_value": "{token} needs a value",
    "cmd_level": "bad flight level '{value}'",
    "cmd_relative": "relative turns need TL/TR",
    "cmd_heading": "bad heading '{value}'",
    "cmd_speed": "bad speed '{value}'",
    "unknown_decision": "unknown decision {id}",
    "decision_belongs": "{id} belongs to {holders}",
    "decision_closed": "decision {id} is no longer open",
    "unknown_option": "unknown option '{option}'",
    "option_hidden": "this option is hidden at your coach level: decide yourself, or ask for a hint",
    "aircraft_left": "the aircraft have left",
    # coach and sessions
    "no_open_situation": "no open situation {id} for you",
    "no_such_session": "no such session",
    "no_recording": "no recording for this session",
    "coach_daily_limit": "daily question limit reached, try again tomorrow",
    "coach_busy": "the coach is still answering your previous question",
    # anything without a code of its own
    "error": "{text}",
}

_catalogs = {}


def catalog(lang):
    """The "errors" section of public/locales/<lang>.json (loaded once)."""
    if lang not in _catalogs:
        try:
            path = config.PUBLIC_DIR / "locales" / ("%s.json" % lang)
            _catalogs[lang] = json.loads(path.read_text(encoding="utf-8")).get("errors", {})
        except (OSError, ValueError):
            _catalogs[lang] = {}
    return _catalogs[lang]


def render(code, lang="en", params=None):
    template = (catalog(lang).get(code) if lang != "en" else None) or EN.get(code) or code
    try:
        return template.format(**(params or {}))
    except (KeyError, IndexError, ValueError):
        return template


def pick_lang(accept_language=None, user_lang=None):
    """The caller's language: the first supported one in Accept-Language (the interface sends its
    current language), else the account's language, else English."""
    tags = []
    for i, part in enumerate((accept_language or "").split(",")):
        bits = part.strip().split(";")
        if not bits[0]:
            continue
        q = 1.0
        for b in bits[1:]:
            if b.strip().startswith("q="):
                try:
                    q = float(b.strip()[2:])
                except ValueError:
                    q = 0.0
        tags.append((-q, i, bits[0].strip().lower()[:2]))
    for _, _, tag in sorted(tags):
        if tag in LANGS:
            return tag
    return user_lang if user_lang in LANGS else "en"


class UserError(Exception):
    """An error for the user: a code, its parameters and an HTTP status."""
    status = 400

    def __init__(self, code, status=None, **params):
        self.code = code
        self.params = params
        if status is not None:
            self.status = status
        super().__init__(render(code, "en", params))

    def text(self, lang):
        return render(self.code, lang, self.params)


class Invalid(UserError, ValueError):
    status = 400


class Denied(UserError, PermissionError):
    status = 403


class Busy(UserError, OverflowError):
    status = 429
