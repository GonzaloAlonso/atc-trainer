"""English templates for every coach message.

Messages travel as {"key", "p", "text"}: the UI renders `key` with `p` in the user's language
(public/locales/*.json, "coach" section, same keys and placeholders); agents, logs and the
language model read `text`. A parameter may itself be a message ({"key", "p"}), rendered first.
"""

T = {
    # why an option of a decision point is, or isn't, the answer
    "why.monitor_ok": "No action needed: they pass {h} NM apart.",
    "why.monitor_ok_v": "No action needed: they stay vertically separated.",
    "why.monitor_bad": "Doing nothing loses separation in {t} s.",
    "why.best_h": "Resolves it with the smallest change: {h} NM at the closest point.",
    "why.best_v": "Resolves it with the smallest change: 1000 ft or more vertically.",
    "why.least_bad": "Nothing keeps full separation; this shortens the loss to {d} s.",
    "why.costlier": "Also safe, but a bigger change than {best}.",
    "why.tighter": "Safe, but tighter: {h} NM at the closest point.",
    "why.equal_h": "Just as good: an equally small change, {h} NM at the closest point.",
    "why.equal_v": "Just as good: an equally small change, 1000 ft or more vertically.",
    "why.unsafe": "Still loses separation, in {t} s ({d} s below the minima).",
    "why.approve_ok": "Granting it is safe for the next 4 minutes.",
    "why.approve_bad": "Granting it now loses separation with {other} in {t} s.",
    "why.intermediate_ok": "The full request conflicts; this level is safe for now.",
    "why.intermediate": "A partial step: safe, but the pilot still wants more.",
    "why.deny_ok": "Keeps separation; grant it later, once {other} is clear.",
    "why.deny_worse": "Safe, but the request can be granted: the pilot keeps waiting.",

    # feedback on a graded decision
    "fb.los": "Separation was lost ({h} NM, {v} ft). Act as soon as the conflict shows: you had {lead} s.",
    "fb.no_action_los": "No clearance was given and separation was lost. {best} would have kept them apart.",
    "fb.late": "Resolved, but late: only {lead} s before the predicted loss. Aim for 90 s or more.",
    "fb.tight": "Separated, but only just: {h} NM and {v} ft at the closest point. Act earlier for a clear margin.",
    "fb.costly": "Safe, but a bigger change than needed: {best} would have done it.",
    "fb.extra": "Safe, but you used {n} clearances where one was enough.",
    "fb.unneeded": "No action was needed: they would have passed {h} NM apart.",
    "fb.good_h": "Well done: resolved {lead} s ahead, {h} NM at the closest point.",
    "fb.good_v": "Well done: resolved {lead} s ahead with vertical separation.",
    "fb.monitor_good": "Right call: no action was needed and they passed safely.",
    "fb.req_granted": "Request granted safely after {rt} s.",
    "fb.req_slow": "Granted safely, but the pilot waited {rt} s. Answer within a minute.",
    "fb.req_denied_ok": "Good judgement: granting it would have caused a conflict.",
    "fb.req_denied_bad": "It could have been granted safely; the pilot keeps waiting.",
    "fb.req_partial": "A safe partial step, but the full request could have been granted.",
    "fb.req_unsafe": "Your answer led to a conflict with {other}. Check the level is free first.",
    "fb.req_expired": "The request went unanswered.",
    "fb.demo": "Demonstration by the AI: not graded.",

    # progressive hints: 1 where to look, 2 what is wrong, 3 a solution
    "hint.c1": "Look at {a} and {b}.",
    "hint.c2": "{a} (FL{fa}) and {b} (FL{fb}) will be {h} NM apart in {t} s: under the {min} NM minimum "
               "with less than 1000 ft between them.",
    "hint.c2_clear": "{a} and {b} are predicted to stay separated: {h} NM at the closest point.",
    "hint.r1_level": "{a} is waiting for an answer: it wants FL{fl}.",
    "hint.r1_route": "{a} is waiting for an answer: it wants to resume its own navigation.",
    "hint.r2_ok": "It is free: granting it keeps separation.",
    "hint.r2_bad": "It is not free: {other} is in the way.",
    "hint.solution": "Try: {option}. {why}",

    # narration while the AI demonstrates
    "narr.act": "{option}: {why}",

    # built-in debrief (no language model)
    "db.verdict": "Average grade {avg} ({letter}) over {n} decisions.",
    "db.empty": "No decisions were graded in this session.",
    "db.los": "Separation was lost {n} time(s): that is the first thing to fix.",
    "db.clean": "No loss of separation.",
    "db.best": "Best decision: {cs} ({letter}).",
    "db.review": "To review: {cs}. {fb}",
    "db.hints": "Hints used: {n}. Try the next attempt with fewer.",
    "db.next_pass": "Next: the next drill, or this one at a stricter coach level.",
    "db.next_retry": "Next: rewind to the situation you want to improve and try it again.",
    "ask.unavailable": "The language coach is not set up on this server. The cards above explain each situation.",
}


def render(key, p=None):
    """English text of a message; nested messages in the parameters are rendered first."""
    p = {k: (render(v["key"], v.get("p")) if isinstance(v, dict) and "key" in v else v)
         for k, v in (p or {}).items()}
    try:
        return T[key].format(**p)
    except (KeyError, IndexError, ValueError):
        return key


def msg(key, **p):
    return {"key": key, "p": p, "text": render(key, p)}
