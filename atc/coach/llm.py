"""Plain-language coaching: session debriefs and answers to a trainee's questions.

Providers (VISOR_COACH_PROVIDER):
  claude    Claude through the official Anthropic SDK (ANTHROPIC_API_KEY, VISOR_COACH_MODEL,
            default claude-opus-5-5). Grounded on the session's graded journal, in the trainee's
            language. Server-side fallback ("default") is on, so a policy decline is retried on
            the model Anthropic recommends instead of failing.
  template  no language model: a short structured debrief from templates; free questions are
            not answered. Also what the coach falls back to when Claude is unavailable.

The coach only explains and teaches. It never issues clearances: nothing it writes reaches the
simulation, and the prompt says so.
"""

import json
import logging
import os

from .. import config
from .text import msg

log = logging.getLogger("visor.coach")

LANGS = {"en": "English", "de": "German", "es": "Spanish"}

SYSTEM = """You are the instructor inside ATC Trainer, a 3D air traffic control training simulator. \
You coach student and prospective air traffic controllers on their decisions in simulated \
en-route airspace over Europe.

You receive a session record in JSON: the exercise or context, a summary, and every graded \
decision (a "situation"): the aircraft involved with their flight levels, the options the \
simulator evaluated by fast-time prediction (each with its predicted closest approach and whether \
separation would be lost), the reference answer chosen by the simulator's rule-based AI, what the \
trainee actually did and when, what happened afterwards, the hints used, and the grade.

How the simulator grades a situation (out of 100):
- Safety, 50 points: separation kept. Minima are 5 NM horizontally or 1000 ft vertically \
(3 NM when both aircraft are below FL100). A loss of separation caps the grade at 20.
- Timeliness, 25 points: how early the trainee acted before the predicted loss of separation. \
The short-term conflict alert (STCA) warns about 2 minutes ahead; acting 90 seconds or more \
before the loss earns full marks.
- Efficiency, 25 points: the smallest change that works. A 1000 ft level change counts as the \
smallest change, a 30 degree turn is larger, several clearances where one was enough cost points, \
and so does acting when no action was needed.
- Hints cost 3, 7 or 15 points (where to look, what is wrong, a solution).
Letters: A from 85, B from 70, C from 55, D from 40, E below.

Controller practice the simulator rewards, which you should reinforce:
- Scan early and act early: detect converging traffic before the alert fires.
- Prefer a vertical solution when a level is free; it is usually the smallest, clearest change.
- Issue one clear, sufficient clearance rather than several small ones.
- Grant pilot requests when the requested level is free, promptly; deny or give an intermediate \
level when it is not, and grant the rest once the traffic is clear.
- Monitor rather than intervene when the prediction shows the aircraft will pass safely.

Rules for your answers:
- Use only the session record. Never invent aircraft, times, levels, events or numbers; if the \
record does not contain something, say so briefly.
- Refer to situations by callsigns and simulation time (UTC, hh:mm:ss) so the trainee can find \
them in the replay.
- Be encouraging but honest and specific. Explain why, using the predicted and actual outcomes.
- Keep radio phraseology in standard ICAO English (for example "climb flight level 360") even \
when you write in another language.
- This is a simplified training simulator. When asked about real-world procedures, give general \
textbook knowledge and say that the local rules of the air navigation service provider prevail. \
Never present anything as operational guidance.
- You never control traffic and never issue clearances; you only explain and teach.
- Plain text with short paragraphs or simple "- " bullet lines. No headings, tables or bold.

A debrief has four parts, under 250 words in total: one line with the overall verdict; up to \
three things that went well; up to three things to improve, each tied to a specific situation; \
one concrete thing to practise next (for example a drill, or the same exercise at a lower coach \
level). An answer to a question stays under 150 words unless the trainee asks for more."""


class CoachUnavailable(Exception):
    pass


def session_record(session, entries, exercise_title=None):
    """The grounding for the language model: the session and its graded situations, without the
    trainee's identity."""
    def clock(t):
        import time as _t
        return _t.strftime("%H:%M:%S", _t.gmtime(t)) if t else None

    situations = []
    for e in entries:
        g = e.get("grade") or {}
        situations.append({
            "id": e["id"], "kind": e["kind"], "opened": clock(e["opened_t"]), "closed": clock(e.get("closed_t")),
            "aircraft": [{k: a.get(k) for k in ("callsign", "fl", "cleared_fl", "heading", "gs_kt", "vs_fpm")}
                         for a in e.get("aircraft", [])],
            "situation": e.get("situation"),
            "options": [{"label": o["label"], "predicted": o["predicted"], "why": (o.get("why") or {}).get("text")}
                        for o in e.get("options", [])],
            "reference_answer": (e.get("best") or {}).get("label"),
            "trainee_actions": [{"at": clock(a["t"]), "said": a["phrases"], "lead_s": a.get("lead_s")}
                                for a in e.get("actions", [])],
            "trainee_response": e.get("response") and {"at": clock(e["response"]["t"]),
                                                      "role": e["response"]["role"],
                                                      "said": e["response"].get("phrases")},
            "outcome": e.get("outcome"), "actual": e.get("actual"),
            "handled_by_ai": e.get("by_ai", False),
            "hints_used": e.get("hint_tier", 0),
            "grade": {"score": g.get("score"), "letter": g.get("letter"), "parts": g.get("parts"),
                      "feedback": (g.get("feedback") or {}).get("text")},
            "feedback_msg": g.get("feedback") and {k: g["feedback"][k] for k in ("key", "p")},
        })
    return {
        "context": session.get("context"), "exercise": exercise_title or session.get("exercise"),
        "summary": session.get("summary"), "situations": situations,
    }


# ---------------------------------------------------------------------------- template
class TemplateProvider:
    name = "template"
    available = True

    def debrief(self, record, lang):
        s = record.get("summary") or {}
        sits = [x for x in record["situations"] if x["grade"]["score"] is not None]
        out = []
        if s.get("average") is not None:
            out.append(msg("db.verdict", avg=s["average"], letter=s["letter"], n=s.get("graded", len(sits))))
        else:
            out.append(msg("db.empty"))
        out.append(msg("db.los", n=s["los"]) if s.get("los") else msg("db.clean"))
        if sits:
            best = max(sits, key=lambda x: x["grade"]["score"])
            worst = min(sits, key=lambda x: x["grade"]["score"])
            out.append(msg("db.best", cs=" / ".join(a["callsign"] for a in best["aircraft"]),
                           letter=best["grade"]["letter"]))
            if worst is not best and worst["grade"]["score"] < 70:
                out.append(msg("db.review", cs=" / ".join(a["callsign"] for a in worst["aircraft"]),
                               fb=worst.get("feedback_msg") or worst["grade"]["feedback"] or ""))
        if s.get("hints"):
            out.append(msg("db.hints", n=s["hints"]))
        out.append(msg("db.next_pass") if s.get("passed") else msg("db.next_retry"))
        return {"provider": self.name, "text": None, "messages": out}

    def answer(self, question, record, history, lang):
        return {"provider": self.name, "text": None, "messages": [msg("ask.unavailable")]}


# ---------------------------------------------------------------------------- Claude
class ClaudeProvider:
    name = "claude"

    def __init__(self, client=None, model=None, effort=None, timeout=None):
        self._client = client
        self.model = model or config.COACH_MODEL
        self.effort = effort or config.COACH_EFFORT
        self.timeout = timeout or config.COACH_TIMEOUT_S
        self.available = client is not None or bool(os.environ.get("ANTHROPIC_API_KEY")
                                                    or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
        self.last_error = None

    def client(self):
        if self._client is None:
            import anthropic                  # imported on first use: the coach works without it
            self._client = anthropic.Anthropic(timeout=self.timeout, max_retries=2)
        return self._client

    def _ask(self, messages):
        try:
            import anthropic
            errors = (anthropic.APIError,)
        except ImportError:                     # a test double needs no SDK
            errors = ()
        try:
            resp = self.client().beta.messages.create(
                model=self.model,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={"effort": self.effort},
                system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
                messages=messages,
            )
        except errors as exc:                   # timeouts, rate limits, 5xx, network (after retries)
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
            raise CoachUnavailable(self.last_error)
        if resp.stop_reason == "refusal":
            self.last_error = "refused"
            raise CoachUnavailable("the coach declined this request")
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
        if not text:
            raise CoachUnavailable("empty answer")
        self.last_error = None
        return text

    @staticmethod
    def _grounding(record, lang):
        return [{"type": "text", "cache_control": {"type": "ephemeral"},
                 "text": "Session record:\n<session>\n%s\n</session>\nWrite in %s."
                         % (json.dumps(record, ensure_ascii=False, separators=(",", ":")), LANGS.get(lang, "English"))}]

    def debrief(self, record, lang):
        content = self._grounding(record, lang) + [{"type": "text", "text": "Debrief this session."}]
        return {"provider": self.name, "model": self.model,
                "text": self._ask([{"role": "user", "content": content}]), "messages": []}

    def answer(self, question, record, history, lang):
        """history: earlier [(question, answer)] of this session, oldest first."""
        messages = []
        for i, (q, a) in enumerate(history):
            content = ([*self._grounding(record, lang), {"type": "text", "text": q}] if i == 0
                       else [{"type": "text", "text": q}])
            messages.append({"role": "user", "content": content})
            messages.append({"role": "assistant", "content": a})
        tail = [{"type": "text", "text": question}]
        messages.append({"role": "user", "content": (self._grounding(record, lang) + tail) if not history else tail})
        return {"provider": self.name, "model": self.model, "text": self._ask(messages), "messages": []}


def make_provider(name=None, client=None):
    name = (name or config.COACH_PROVIDER).lower()
    if name == "claude":
        p = ClaudeProvider(client=client)
        if p.available:
            return p
    return TemplateProvider()


class CoachService:
    """Chooses Claude when configured and falls back to the templates on any failure."""

    def __init__(self, provider=None):
        self.provider = provider or make_provider()
        self.fallback = TemplateProvider()

    def status(self):
        p = self.provider
        return {"provider": p.name, "model": getattr(p, "model", None),
                "available": p.name != "template", "last_error": getattr(p, "last_error", None)}

    def debrief(self, record, lang):
        try:
            return self.provider.debrief(record, lang)
        except CoachUnavailable as exc:
            log.warning("coach debrief fell back to templates: %s", exc)
            out = self.fallback.debrief(record, lang)
            out["fallback_reason"] = str(exc)
            return out

    def answer(self, question, record, history, lang):
        try:
            return self.provider.answer(question, record, history, lang)
        except CoachUnavailable as exc:
            log.warning("coach answer failed: %s", exc)
            out = self.fallback.answer(question, record, history, lang)
            out["fallback_reason"] = str(exc)
            return out
