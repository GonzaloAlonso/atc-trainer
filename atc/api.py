"""HTTP + WebSocket API. The browser UI and external AI agents use exactly the same endpoints.

Interactive documentation (OpenAPI) is served at /docs.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
import time
from typing import Any, Dict, List, Literal, Optional

from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, config, exercises, sectors
from .about import about as about_info, copyright_line
from .auth import AuthError, AuthStore, LoginThrottle
from .clearances import KINDS, Clearance, parse_command
from .coach import DEFAULT_LEVEL, LEVELS
from .coach.journal import summarize
from .coach.llm import CoachService, session_record
from .coach.store import CoachStore
from .control import ControlError, display, human
from .engine import Engine
from .errors import UserError, pick_lang, render
from .exercises.runner import ExerciseSandbox
from .navdata import NavData
from .power import PowerManager
from .recorder import Recorder
from .store import Store
from .training import TutorialManager

log = logging.getLogger("visor.api")


# ---------------------------------------------------------------------------- request models
class ClearanceIn(BaseModel):
    kind: Literal["CLIMB", "DESCEND", "LEVEL", "HEADING", "TURN", "DIRECT", "SPEED", "RESUME"]
    value: Optional[Any] = Field(None, description="FL, heading, degrees, fix ident or IAS (kt)")
    direction: Optional[Literal["L", "R"]] = None


class ClearanceRequest(BaseModel):
    aircraft: str = Field(..., description="Callsign or ICAO24 address")
    clearances: List[ClearanceIn]
    issuer: str = Field("human", description='"human" (recorded as the logged-in user) or "ai:<agent name>"')


class CommandRequest(BaseModel):
    text: str = Field(..., examples=["DLH4AB C 370 TL 270"])
    issuer: str = "human"


class SimControl(BaseModel):
    action: Literal["pause", "resume", "speed", "reset", "lockstep", "step"]
    speed: Optional[float] = None
    mode: Optional[Literal["live", "replay"]] = None
    start: Optional[float] = Field(None, description="Replay start, epoch seconds")
    hours_ago: Optional[float] = Field(None, description="Replay start relative to now")
    lockstep: Optional[bool] = None
    dt: Optional[float] = Field(None, description="Seconds to advance for action=step")


class SectorTake(BaseModel):
    force: bool = Field(False, description="Admins: take it even if another controller holds it")


class SectorAI(BaseModel):
    agent: str = "rules"
    force: bool = False


class AiRequest(BaseModel):
    mode: Optional[Literal["off", "advisory", "autonomous"]] = None
    agent: Optional[str] = None


class DecisionAnswer(BaseModel):
    answers: Dict[str, Any] = Field(..., description='At least {"action": "<option id>"}')
    by: str = "human"


class LoginRequest(BaseModel):
    username: str
    password: str


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


class UserCreate(BaseModel):
    username: str
    password: str
    role: Literal["admin", "controller"] = "controller"
    must_change: bool = True


class UserUpdate(BaseModel):
    role: Optional[Literal["admin", "controller"]] = None
    password: Optional[str] = None
    disabled: Optional[bool] = None
    must_change: Optional[bool] = None
    tutorial_reset: bool = False


class TutorialProgress(BaseModel):
    step: int = Field(..., ge=0, le=100)


class TutorialScenario(BaseModel):
    event: Literal["conflict"]


CoachLevelName = Literal["off", "evaluate", "hints", "advise", "demonstrate"]


class PrefsRequest(BaseModel):
    lang: Optional[Literal["en", "de", "es"]] = None
    coach_level: Optional[CoachLevelName] = None


class CoachLevelRequest(BaseModel):
    level: CoachLevelName = Field(..., description="off | evaluate | hints | advise | demonstrate")


class HintRequest(BaseModel):
    decision: str = Field(..., description="Decision point id, e.g. D12")


class ProbeRequest(BaseModel):
    aircraft: Optional[str] = Field(None, description="Callsign or ICAO24 (with clearances)")
    clearances: Optional[List[ClearanceIn]] = None
    command: Optional[str] = Field(None, examples=["DLH4AB C 360"], description="Or ATC shorthand")


class RewindRequest(BaseModel):
    to_t: Optional[float] = Field(None, description="Simulation time to go back to (epoch seconds)")
    entry: Optional[str] = Field(None, description="Or a graded situation id: go back to just before it")
    seconds_before: float = 30.0


class DebriefRequest(BaseModel):
    lang: Literal["en", "de", "es"] = "en"


class AskRequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=1000)
    lang: Literal["en", "de", "es"] = "en"


class Problem(Exception):
    """An HTTP error, answered in the caller's language (see atc/errors.py)."""

    def __init__(self, status, code, **params):
        super().__init__(render(code, "en", params))
        self.status, self.code, self.params = status, code, params

    @classmethod
    def of(cls, status, exc):
        """From a coded error, or (fallback) any exception's English text."""
        if isinstance(exc, UserError):
            return cls(status, exc.code, **exc.params)
        return cls(status, "error", text=str(exc))


COOKIE = "visor_session"
# Reachable without a session: the login page and what it needs, plus the health probe.
PUBLIC_EXACT = {"/login", "/login.html", "/api/health", "/api/auth/login", "/favicon.ico", "/js/login.js",
                "/js/radar-scope.js", "/js/i18n.js"}
PUBLIC_PREFIX = ("/css/", "/locales/")
ADMIN_PATHS = {"/admin", "/admin.html"}
# Allowed while a password change is pending
CHANGE_ALLOWED = {"/api/auth/me", "/api/auth/password", "/api/auth/logout", "/api/auth/prefs"}


def _is_api(path):
    return path.startswith("/api/") or path in ("/docs", "/redoc", "/openapi.json")


def _issuer(user, requested):
    """Humans are identified by their account; external agents may name themselves "ai:<name>"."""
    if requested and requested.startswith("ai:"):
        return requested
    return "human:" + user["username"]


def _public_user(u):
    out = {k: u[k] for k in ("id", "username", "role", "disabled", "must_change",
                             "created", "updated", "last_login", "tutorial_state", "tutorial_step", "lang")}
    out["coach_level"] = u.get("coach_level") or DEFAULT_LEVEL
    return out


def _level(user):
    return user.get("coach_level") or DEFAULT_LEVEL


# ---------------------------------------------------------------------------- app
def create_app():
    store = Store()
    navdata = NavData()
    recorder = Recorder(store)
    coach_store = CoachStore()
    engine = Engine(store, navdata, sector_idle_s=config.SECTOR_IDLE_S, coach_store=coach_store)
    auth = AuthStore()
    auth.bootstrap()
    throttle = LoginThrottle()
    # private sandboxes: the tutorial or an exercise, one per user
    tutorials = TutorialManager(navdata, max_sandboxes=config.MAX_SANDBOXES, coach_store=coach_store)
    coach = CoachService()
    asking = set()          # users waiting for the language coach (one question at a time)
    sockets = {}            # WebSocket -> (sandbox user id or None for live, viewer holder key, kind)
    power = PowerManager(engine, tutorials, config.IDLE_SLEEP_S)

    @asynccontextmanager
    async def lifespan(_app):
        if config.RECORD:
            recorder.start()
        engine.start()
        task = asyncio.get_event_loop().create_task(_broadcast())
        yield
        task.cancel()
        tutorials.stop_all()
        engine.stop()
        recorder.stop()

    app = FastAPI(title="ATC Trainer", version=__version__, lifespan=lifespan,
                  description="Air traffic control simulator on recorded OpenSky data.\n\n"
                              + copyright_line() + " Owner, creator and developer: Gonzalo Alonso.")
    app.state.engine = engine
    app.state.power = power

    # ------------------------------------------------------------------ errors in the user's language
    def _lang(request):
        """The interface sends its language (Accept-Language); agents get the account's, or English."""
        user = getattr(request.state, "user", None)
        return pick_lang(request.headers.get("accept-language"), (user or {}).get("lang"))

    def _error(request, status, code, params=None, **extra):
        params = params or {}
        body = {"detail": render(code, _lang(request), params), "code": code, "params": params}
        body.update(extra)
        return JSONResponse(body, status_code=status)

    @app.exception_handler(Problem)
    async def problem_handler(request: Request, exc: Problem):
        return _error(request, exc.status, exc.code, exc.params)

    @app.exception_handler(UserError)
    async def user_error_handler(request: Request, exc: UserError):
        return _error(request, exc.status, exc.code, exc.params)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        """FastAPI's usual 422 body (detail = the list of problems), plus a readable message."""
        errs = jsonable_encoder(exc.errors())
        fields = ", ".join(sorted({str(e["loc"][-1]) for e in errs if e.get("loc")})) or "?"
        params = {"fields": fields}
        return JSONResponse({"detail": errs, "code": "invalid_request", "params": params,
                             "message": render("invalid_request", _lang(request), params)}, status_code=422)
    app.state.tutorials = tutorials
    app.state.coach_store = coach_store
    app.state.coach = coach

    async def _broadcast():
        last = {}               # ws -> last frame sent
        ticks = 0
        while True:
            await asyncio.sleep(config.TICK_REAL_S)
            ticks += 1
            if ticks % 120 == 0:
                tutorials.reap()
            if ticks % 20 == 0:
                power.check(connected=len(sockets))
            if not sockets:
                continue
            sandboxes = tutorials.boxes()
            for ws, (uid, viewer, kind) in list(sockets.items()):
                box = sandboxes.get(uid) if uid is not None else None
                eng = engine if uid is None else (box.engine if box is not None and box.kind == kind else None)
                if eng is None:                 # sandbox ended (stopped, reaped or replaced)
                    sockets.pop(ws, None)
                    try:
                        await ws.close(code=4404)
                    except Exception:
                        pass
                    continue
                if uid is None:
                    engine.control.seen(viewer[6:])     # an open scope counts as presence
                frame = eng.frame_for(viewer)
                if frame is last.get(ws):
                    continue
                last[ws] = frame
                try:
                    await ws.send_text(frame)
                except Exception:
                    sockets.pop(ws, None)
                    last.pop(ws, None)

    # ------------------------------------------------------------------ authentication
    def _token(headers, cookies):
        authz = headers.get("authorization", "")
        if authz.lower().startswith("bearer "):
            return authz[7:].strip(), True
        return cookies.get(COOKIE), False

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        path = request.url.path
        token, bearer = _token(request.headers, request.cookies)
        user = auth.session_user(token) if token else None
        request.state.user = user
        request.state.token = token
        request.state.bearer = bearer     # agents sign in with a token; the UI uses the cookie
        public = path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIX)

        # Cookie-authenticated state changes must come from our own origin (CSRF defence).
        if (not bearer and request.method not in ("GET", "HEAD", "OPTIONS")
                and request.headers.get("origin")):
            origin_host = urlsplit(request.headers["origin"]).netloc
            host = request.headers.get("x-forwarded-host") or request.headers.get("host")
            if origin_host != host:
                return _error(request, 403, "cross_origin")

        if public:
            return _no_store_ui(path, await call_next(request))
        if user is None:
            if _is_api(path):
                return _error(request, 401, "auth_required")
            nxt = path + ("?" + request.url.query if request.url.query else "")
            return RedirectResponse("/login?next=" + quote(nxt, safe="/"), status_code=302)
        if user["must_change"] and path not in CHANGE_ALLOWED:
            if _is_api(path):
                return _error(request, 403, "password_change_required")
            return RedirectResponse("/login?change=1", status_code=302)
        if power.asleep:
            await asyncio.to_thread(power.activity)     # loading a scenario takes a moment
        else:
            power.activity()
        engine.control.seen(user["username"])
        if engine.coach.levels.get(user["username"]) is None:
            engine.coach.levels[user["username"]] = _level(user)     # the user's saved coach level
        if (path in ADMIN_PATHS or path.startswith("/api/admin/")) and user["role"] != "admin":
            if _is_api(path):
                return _error(request, 403, "admin_only")
            return RedirectResponse("/", status_code=302)
        return _no_store_ui(path, await call_next(request))

    def _no_store_ui(path, response):
        """After an upgrade browsers must fetch the new UI: always revalidate pages and assets
        (cheap with ETags). Map tiles and fonts come from CDNs and are unaffected."""
        if not _is_api(path) and "cache-control" not in response.headers:
            response.headers["Cache-Control"] = "no-cache"
        return response

    def _client_ip(request):
        if config.TRUST_PROXY:
            xff = request.headers.get("x-forwarded-for", "")
            hops = [h.strip() for h in xff.split(",") if h.strip()]
            if hops:
                return hops[-1]
        return request.client.host if request.client else "?"

    def _cookie_secure(request):
        if config.COOKIE_SECURE in ("true", "1", "yes"):
            return True
        if config.COOKIE_SECURE in ("false", "0", "no"):
            return False
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme
        return proto.split(",")[0].strip() == "https"

    @app.get("/api/health", tags=["info"])
    def health():
        """Unauthenticated liveness probe (Docker HEALTHCHECK)."""
        return {"ok": True, "version": __version__, "copyright": copyright_line()}

    @app.get("/api/about", tags=["info"])
    def about():
        """Version, build (commit, date), runtime, copyright and third-party credits."""
        return about_info()

    @app.post("/api/auth/login", tags=["auth"])
    def login(req: LoginRequest, request: Request, response: Response):
        """Sets the session cookie. The returned token also works as `Authorization: Bearer`."""
        ip = _client_ip(request)
        wait = throttle.retry_after(req.username, ip)
        if wait:
            raise Problem(429, "login_throttled", s=int(wait))
        user = auth.authenticate(req.username, req.password)
        if user is None:
            throttle.failed(req.username, ip)
            raise Problem(401, "login_invalid")
        throttle.succeeded(req.username)
        auth.purge_expired()
        token = auth.create_session(user["id"])
        power.activity()
        response.set_cookie(COOKIE, token, max_age=int(config.SESSION_HOURS * 3600), httponly=True,
                            samesite="lax", secure=_cookie_secure(request), path="/")
        return {"user": _public_user(user), "token": token}

    @app.post("/api/auth/logout", tags=["auth"])
    def logout(request: Request, response: Response):
        auth.revoke_session(request.state.token)
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/auth/me", tags=["auth"])
    def me(request: Request):
        return _public_user(request.state.user)

    @app.post("/api/auth/prefs", tags=["auth"])
    def set_prefs(req: PrefsRequest, request: Request):
        """Your interface language and coach level (both are remembered)."""
        user = request.state.user
        try:
            updated = auth.set_prefs(user["id"], lang=req.lang, coach_level=req.coach_level)
        except AuthError as exc:
            raise Problem.of(400, exc)
        if req.coach_level:
            _apply_level(user, req.coach_level)
        return _public_user(updated)

    def _apply_level(user, level):
        engine.set_coach(user["username"], level)
        box = tutorials.get(user["id"])
        if box is not None:
            box.engine.set_coach(user["username"], level)

    @app.post("/api/auth/password", tags=["auth"])
    def change_password(req: PasswordChange, request: Request):
        user = request.state.user
        try:
            updated = auth.change_own_password(user["id"], req.current_password, req.new_password)
        except AuthError as exc:
            raise Problem.of(400, exc)
        # other sessions of this user are signed out; this one stays valid
        token = auth.create_session(user["id"])
        resp = JSONResponse({"user": _public_user(updated)})
        resp.set_cookie(COOKIE, token, max_age=int(config.SESSION_HOURS * 3600), httponly=True,
                        samesite="lax", secure=_cookie_secure(request), path="/")
        return resp

    def _me(request):
        return human(request.state.user["username"])

    def _hides(request):
        """Decision options follow the caller's coach level in the UI (cookie sessions). Agents,
        which sign in with a bearer token, always see every option."""
        return not getattr(request.state, "bearer", False)

    def _is_admin(request):
        return request.state.user["role"] == "admin"

    def _box(request, kind):
        box = tutorials.get(request.state.user["id"], kind=kind)
        if box is None:
            raise Problem(409, "no_tutorial_session" if kind == "tutorial" else "no_exercise_session")
        return box

    def _engine(request):
        """The caller's private sandbox (header X-ATC-Context: tutorial | exercise) or the shared sim."""
        ctx = request.headers.get("x-atc-context")
        if ctx in ("tutorial", "exercise"):
            return _box(request, ctx).engine
        return engine

    # ------------------------------------------------------------------ guided tutorial
    @app.post("/api/tutorial/start", tags=["tutorial"])
    def tutorial_start(request: Request):
        """(Re)create the caller's private training sandbox. Use it with `X-ATC-Context: tutorial`."""
        user = request.state.user
        try:
            box = tutorials.start(user["id"], username=user["username"], level=_level(user))
        except OverflowError as exc:
            raise Problem.of(429, exc)
        return {"started": box.created, "sector": "ALP-U", "user": _public_user(auth.get_user(user["id"]))}

    @app.post("/api/tutorial/stop", tags=["tutorial"])
    def tutorial_stop(request: Request):
        uid = request.state.user["id"]
        return {"stopped": tutorials.get(uid, kind="tutorial") is not None and tutorials.stop(uid)}

    @app.post("/api/tutorial/scenario", tags=["tutorial"])
    def tutorial_scenario(req: TutorialScenario, request: Request):
        """Trigger a scripted situation in the sandbox (e.g. the head-on conflict lesson)."""
        return _box(request, "tutorial").inject_conflict()

    @app.post("/api/tutorial/progress", tags=["tutorial"])
    def tutorial_progress(req: TutorialProgress, request: Request):
        return _public_user(auth.set_tutorial(request.state.user["id"], step=req.step))

    @app.post("/api/tutorial/complete", tags=["tutorial"])
    def tutorial_complete(request: Request):
        uid = request.state.user["id"]
        if tutorials.get(uid, kind="tutorial") is not None:
            tutorials.stop(uid)
        return _public_user(auth.set_tutorial(uid, state="completed", step=0))

    @app.post("/api/tutorial/dismiss", tags=["tutorial"])
    def tutorial_dismiss(request: Request):
        """"Don't remind me": stop offering the tutorial at sign-in (it stays in the menu)."""
        return _public_user(auth.set_tutorial(request.state.user["id"], state="dismissed"))

    # ------------------------------------------------------------------ user administration
    @app.get("/api/admin/users", tags=["admin"])
    def list_users():
        return [_public_user(u) for u in auth.list_users()]

    @app.post("/api/admin/users", tags=["admin"], status_code=201)
    def create_user(req: UserCreate):
        try:
            return _public_user(auth.create_user(req.username, req.password, req.role, req.must_change))
        except AuthError as exc:
            raise Problem.of(400, exc)

    @app.patch("/api/admin/users/{user_id}", tags=["admin"])
    def update_user(user_id: int, req: UserUpdate, request: Request):
        try:
            return _public_user(auth.update_user(
                user_id, actor_id=request.state.user["id"], role=req.role, password=req.password,
                disabled=req.disabled, must_change=req.must_change, tutorial_reset=req.tutorial_reset))
        except AuthError as exc:
            raise Problem.of(exc.status, exc)

    @app.delete("/api/admin/users/{user_id}", tags=["admin"])
    def delete_user(user_id: int, request: Request):
        try:
            auth.delete_user(user_id, actor_id=request.state.user["id"])
        except AuthError as exc:
            raise Problem.of(exc.status, exc)
        return {"ok": True}

    # ------------------------------------------------------------------ realtime
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        token, _ = _token(ws.headers, ws.cookies)
        user = auth.session_user(token)
        await ws.accept()
        if user is None or user["must_change"]:
            await ws.close(code=4401)
            return
        uid, kind = None, None
        src = engine
        ctx = ws.query_params.get("ctx")
        if ctx in ("tutorial", "exercise"):
            box = tutorials.get(user["id"], kind=ctx)
            if box is None:
                await ws.close(code=4404)
                return
            uid, kind, src = user["id"], ctx, box.engine
        viewer = human(user["username"])
        if uid is None:
            await asyncio.to_thread(power.activity)
            engine.coach.levels.setdefault(user["username"], _level(user))
        sockets[ws] = (uid, viewer, kind)
        try:
            await ws.send_text(src.frame_for(viewer))
            while True:
                await ws.receive_text()      # client messages are ignored; use REST for actions
        except WebSocketDisconnect:
            pass
        finally:
            sockets.pop(ws, None)

    # ------------------------------------------------------------------ status & static data
    @app.get("/api/status", tags=["info"])
    def status(request: Request):
        eng = _engine(request)
        return {"version": __version__, "recording": config.RECORD,
                "recorder": recorder.status(), "ai": eng.ai_status(request.state.user["username"]),
                "sim": {"t": eng.t, "mode": eng.mode, "speed": eng.speed,
                        "paused": eng.paused, "lockstep": eng.lockstep,
                        "aircraft": len(eng.aircraft)},
                "power": power.status(), "coach": coach.status(),
                "area": config.EUROPE, "now": time.time()}

    @app.get("/api/navdata", tags=["info"])
    def get_navdata():
        return {"fixes": [[f["ident"], f["kind"], round(f["lat"], 4), round(f["lon"], 4), f["name"]]
                          for f in navdata.fixes]}

    # ------------------------------------------------------------------ sectorization
    @app.get("/api/sectors", tags=["sectors"])
    def get_sectors(request: Request):
        """Sector catalogue (regions × vertical layers) with who controls each sector now."""
        eng = _engine(request)
        cat = sectors.catalogue()
        holders = eng.control.snapshot()
        for s in cat["sectors"]:
            h = holders.get(s["id"])
            s["holder"], s["holder_name"] = h, display(h)
        cat["me"] = {"holder": _me(request), "sectors": eng.control.sectors_of(_me(request))}
        return cat

    def _sector_call(fn):
        try:
            return fn()
        except ControlError as exc:
            raise Problem.of(exc.status, exc)
        except ValueError as exc:
            raise Problem.of(400, exc)

    @app.post("/api/sectors/{sector_id}/take", tags=["sectors"])
    def take_sector(sector_id: str, request: Request, req: Optional[SectorTake] = None):
        """Take responsibility for a sector (you may hold several). Admins can force a takeover."""
        eng = _engine(request)
        force = bool(req and req.force and _is_admin(request))
        mine = _sector_call(lambda: eng.take_sector(sector_id, _me(request), force))
        return {"sectors": mine}

    @app.post("/api/sectors/{sector_id}/release", tags=["sectors"])
    def release_sector(sector_id: str, request: Request):
        """Release your sector (admins may release anyone's)."""
        eng = _engine(request)
        mine = _sector_call(lambda: eng.release_sector(sector_id, _me(request), force=_is_admin(request)))
        return {"sectors": mine}

    @app.post("/api/sectors/{sector_id}/assign-ai", tags=["sectors"])
    def assign_ai_sector(sector_id: str, request: Request, req: Optional[SectorAI] = None):
        """Hand a free sector, or one of yours, to an AI agent that controls it autonomously."""
        eng = _engine(request)
        req = req or SectorAI()
        _sector_call(lambda: eng.assign_ai(sector_id, req.agent, _me(request),
                                           force=bool(req.force and _is_admin(request))))
        return {"sector": sector_id, "holder": "ai:" + req.agent}

    # ------------------------------------------------------------------ observation
    @app.get("/api/observation", tags=["agent"])
    def observation(request: Request, mine_only: bool = False, scope: str = "mine"):
        """Typed state: all traffic (with sector and holder), plus the conflicts and decision
        points you are responsible for. Admins may pass scope=all to see every issue."""
        eng = _engine(request)
        viewer = None if scope == "all" and _is_admin(request) else _me(request)
        return eng.observation(viewer, mine_only, hide=_hides(request))

    @app.get("/api/aircraft/{ident}", tags=["agent"])
    def aircraft(ident: str, request: Request):
        eng = _engine(request)
        d = eng.aircraft_detail(ident, _me(request))
        if d is None:
            raise Problem(404, "no_such_aircraft")
        return d

    @app.get("/api/events", tags=["agent"])
    def events(request: Request, since: int = 0):
        eng = _engine(request)
        return eng.events_since(since, _me(request))

    # ------------------------------------------------------------------ actions
    @app.post("/api/clearance", tags=["agent"])
    def clearance(req: ClearanceRequest, request: Request):
        eng = _engine(request)
        try:
            clrs = [Clearance(c.kind, c.value, c.direction) for c in req.clearances]
            return eng.issue(req.aircraft, clrs, _issuer(request.state.user, req.issuer), actor=_me(request))
        except PermissionError as exc:
            raise Problem.of(403, exc)
        except ValueError as exc:
            raise Problem.of(400, exc)

    @app.post("/api/command", tags=["agent"])
    def command(req: CommandRequest, request: Request):
        """ATC shorthand, e.g. `DLH4AB C 370`, `EZY12 TL 270`, `RYR1 DCT KPT`, `AFR7 RON`."""
        eng = _engine(request)
        try:
            return eng.command(req.text, _issuer(request.state.user, req.issuer), actor=_me(request))
        except PermissionError as exc:
            raise Problem.of(403, exc)
        except ValueError as exc:
            raise Problem.of(400, exc)

    @app.get("/api/decisions", tags=["agent"])
    def list_decisions(request: Request, status: Optional[str] = "open"):
        eng = _engine(request)
        me_ = _me(request)
        with eng.lock:
            return [dp.to_dict(eng.reveal(me_, dp) if _hides(request) else None) for dp in eng.visible_decisions(me_)
                    if status is None or dp.status == status]

    @app.post("/api/decisions/{dp_id}", tags=["agent"])
    def answer(dp_id: str, req: DecisionAnswer, request: Request):
        eng = _engine(request)
        try:
            return eng.answer_decision(dp_id, req.answers, _issuer(request.state.user, req.by), actor=_me(request),
                                       enforce_level=_hides(request))
        except PermissionError as exc:
            raise Problem.of(403, exc)
        except ValueError as exc:
            raise Problem.of(400, exc)

    @app.post("/api/decisions/{dp_id}/dismiss", tags=["agent"])
    def dismiss(dp_id: str, request: Request):
        eng = _engine(request)
        try:
            eng.dismiss_decision(dp_id, actor=_me(request))
        except PermissionError as exc:
            raise Problem.of(403, exc)
        return {"ok": True}

    # ------------------------------------------------------------------ control
    @app.post("/api/sim", tags=["control"])
    def sim(req: SimControl, request: Request):
        """Pause, speed, scenario and lockstep. On the shared simulation these affect everyone,
        so they are admin-only; in your tutorial sandbox they are yours."""
        eng = _engine(request)
        if eng is engine and not _is_admin(request):
            raise Problem(403, "sim_admin_only")
        try:
            if req.action == "pause":
                eng.set_paused(True)
            elif req.action == "resume":
                eng.set_paused(False)
            elif req.action == "speed":
                eng.set_speed(req.speed or 1.0)
            elif req.action == "lockstep":
                eng.set_lockstep(bool(req.lockstep))
            elif req.action == "step":
                if not eng.lockstep:
                    raise Problem(400, "lockstep_first")
                eng.step(max(0.1, min(600.0, req.dt or 5.0)))
                with eng.lock:
                    eng._build_frame()
                return eng.observation(_me(request))
            elif req.action == "reset":
                if eng is not engine:
                    raise Problem(400, "sandbox_reset")
                start = req.start
                if start is None and req.hours_ago is not None:
                    start = time.time() - req.hours_ago * 3600
                eng.reset(req.mode or "live", start)
        except ValueError as exc:
            raise Problem.of(400, exc)
        return status(request)

    @app.post("/api/ai", tags=["control"])
    def set_ai(req: AiRequest, request: Request):
        """Your AI assistance (kept for compatibility): off / advisory / autonomous are the coach
        levels off / advise / demonstrate, and are remembered like them."""
        eng = _engine(request)
        user = request.state.user
        try:
            out = eng.set_ai(user["username"], req.mode, req.agent)
        except ValueError as exc:
            raise Problem.of(400, exc)
        if req.mode:
            level = Engine._MODE_LEVEL[req.mode]
            auth.set_prefs(user["id"], coach_level=level)
            _apply_level(user, level)
        return out

    @app.post("/api/sector", include_in_schema=False)
    def legacy_sector():
        """Removed in 2.0. Pages cached from 1.x still call it: tell them to reload."""
        raise Problem(410, "page_outdated", version=__version__)

    @app.get("/api/schema", tags=["agent"])
    def schema():
        """Machine-readable description of the action space for agent builders."""
        return {
            "clearance_kinds": list(KINDS) + ["LEVEL"],
            "clearance": ClearanceIn.model_json_schema(),
            "decision_answer": DecisionAnswer.model_json_schema(),
            "command_grammar": {
                "climb": "CS C <FL>", "descend": "CS D <FL>", "level (auto)": "CS FL <FL>",
                "turn left/right to heading": "CS TL <HDG> | CS TR <HDG>",
                "fly heading": "CS H <HDG>", "turn by degrees": "CS TL 30D | CS TR 20D",
                "direct": "CS DCT <FIX>", "speed": "CS S <IAS>", "resume": "CS RON",
            },
            "separation": {"h_nm": config.SEP_H_NM, "v_ft": config.SEP_V_FT},
            "coach": {
                "levels": list(LEVELS),
                "note": "In the UI, decision options follow the user's coach level (hidden at evaluate "
                        "and hints). Requests with a bearer token (agents) always get every option.",
                "hint": "POST /api/coach/hint {decision}",
                "probe": "POST /api/probe {aircraft, clearances} | {command: 'CS C 360'}",
                "whatif": "GET /api/decisions/{id}/whatif?option=o3",
            },
            "exercises": [e["id"] for e in exercises.catalogue()],
        }

    # ------------------------------------------------------------------ AI coach
    @app.get("/api/coach", tags=["coach"])
    def coach_status(request: Request):
        """Your coach level, the levels, and which language coach is configured."""
        eng = _engine(request)
        with eng.lock:
            view = eng.coach.view(request.state.user["username"])
        return {**view, "levels": list(LEVELS), "language_coach": coach.status()}

    @app.post("/api/coach", tags=["coach"])
    def coach_level(req: CoachLevelRequest, request: Request):
        """Set your coach level: off | evaluate | hints | advise | demonstrate (remembered)."""
        user = request.state.user
        auth.set_prefs(user["id"], coach_level=req.level)
        _apply_level(user, req.level)
        eng = _engine(request)
        with eng.lock:
            return eng.coach.view(user["username"])

    @app.post("/api/coach/hint", tags=["coach"])
    def coach_hint(req: HintRequest, request: Request):
        """The next hint for one of your open situations: 1 where to look, 2 what is wrong,
        3 a solution (which also reveals that option in your Decisions list). Hints cost points."""
        eng = _engine(request)
        try:
            return eng.hint(request.state.user["username"], req.decision)
        except ValueError as exc:
            raise Problem.of(404, exc)

    @app.post("/api/probe", tags=["agent"])
    def probe(req: ProbeRequest, request: Request):
        """What would happen: fly a candidate clearance ahead (with the traffic it is in conflict
        with) without issuing it. Returns predicted 3D tracks, the closest approach, and the
        same prediction without the clearance for comparison."""
        eng = _engine(request)
        try:
            if req.command:
                callsign, clrs = parse_command(req.command)
            else:
                callsign = req.aircraft
                clrs = [Clearance(c.kind, c.value, c.direction) for c in req.clearances or []]
            if not callsign or not clrs:
                raise Problem(400, "probe_input")
            return eng.probe(callsign, clrs, actor=_me(request))
        except ValueError as exc:
            raise Problem.of(400, exc)

    @app.get("/api/decisions/{dp_id}/whatif", tags=["agent"])
    def whatif(dp_id: str, option: str, request: Request):
        """3D prediction of one option of your decision point (tracks and closest approach)."""
        eng = _engine(request)
        try:
            return eng.whatif(dp_id, option, _me(request), enforce_level=_hides(request))
        except PermissionError as exc:
            raise Problem.of(403, exc)
        except ValueError as exc:
            raise Problem.of(404, exc)

    # ------------------------------------------------------------------ exercises
    def _best_results(username):
        best = {}
        for s in coach_store.sessions(username, limit=500):
            summ = s.get("summary") or {}
            if s["context"] != "exercise" or s["status"] != "completed" or summ.get("average") is None:
                continue
            cur = best.get(s["exercise"])
            if cur is None or summ["average"] > cur["average"]:
                best[s["exercise"]] = {"average": summ["average"], "letter": summ["letter"],
                                       "passed": summ.get("passed"), "session": s["id"]}
        return best

    @app.get("/api/exercises", tags=["exercises"])
    def list_exercises(request: Request):
        """Scripted training situations, with your best completed result for each."""
        best = _best_results(request.state.user["username"])
        return {"exercises": [dict(e, best=best.get(e["id"])) for e in exercises.catalogue()]}

    @app.post("/api/exercises/{ex_id}/start", tags=["exercises"])
    def start_exercise(ex_id: str, request: Request):
        """Start (or restart) an exercise in your private sandbox; it replaces any tutorial or
        exercise you had open. Then use `X-ATC-Context: exercise` (or `/ws?ctx=exercise`)."""
        ex = exercises.BY_ID.get(ex_id)
        if ex is None:
            raise Problem(404, "no_such_exercise")
        user = request.state.user
        try:
            box = tutorials.start(user["id"], factory=lambda: ExerciseSandbox(
                navdata, ex, user["username"], coach_store=coach_store, level=_level(user)))
        except OverflowError as exc:
            raise Problem.of(429, exc)
        return {"exercise": ex.public(), "session": box.session, "sector": ex.sector}

    @app.post("/api/exercise/rewind", tags=["exercises"])
    def rewind_exercise(req: RewindRequest, request: Request):
        """Go back in your exercise and try again: to a time, or to just before a graded situation.
        The attempt so far is kept (with its grades); a new attempt starts."""
        box = _box(request, "exercise")
        to_t = req.to_t
        if req.entry:
            e = next((x for x in box.engine.coach.closed if x["id"] == req.entry), None)
            if e is None:
                raise Problem(404, "no_such_situation")
            to_t = e["opened_t"] - req.seconds_before
        if to_t is None:
            raise Problem(400, "rewind_input")
        try:
            return box.rewind(to_t)
        except ValueError as exc:
            raise Problem.of(400, exc)

    @app.post("/api/exercise/stop", tags=["exercises"])
    def stop_exercise(request: Request):
        uid = request.state.user["id"]
        return {"stopped": tutorials.get(uid, kind="exercise") is not None and tutorials.stop(uid)}

    # ------------------------------------------------------------------ sessions & debriefs
    def _session(sid, request):
        s = coach_store.session(sid)
        user = request.state.user
        if s is None or (s["user"] != user["username"] and user["role"] != "admin"):
            raise Problem(404, "no_such_session")
        return s

    def _live_coach(s):
        """The running coach that owns session s (its recording and open situations), if any."""
        if s["context"] == "live":
            return engine.coach
        for box in tutorials.boxes().values():
            if box.engine.coach.sessions.get(s["user"]) == s["id"]:
                return box.engine.coach
        return None

    def _session_data(s):
        entries = coach_store.entries(s["id"])
        if s.get("summary") is None:
            s["summary"] = summarize(entries)
        return entries

    @app.get("/api/sessions", tags=["coach"])
    def list_sessions(request: Request, user: Optional[str] = None, limit: int = 50):
        """Your coached sessions, newest first (admins: any user's with ?user=)."""
        me_ = request.state.user
        who = user if (user and me_["role"] == "admin") else me_["username"]
        return {"sessions": coach_store.sessions(who, limit=max(1, min(limit, 500)))}

    @app.get("/api/sessions/{sid}", tags=["coach"])
    def get_session(sid: int, request: Request):
        """A session with every graded situation: what happened, what you did, the AI's answer."""
        s = _session(sid, request)
        entries = _session_data(s)
        live = _live_coach(s)
        ex = exercises.BY_ID.get(s["exercise"]) if s.get("exercise") else None
        return {"session": s, "entries": entries, "exercise": ex.public() if ex else None,
                "replay": bool(coach_store.recording(sid) is not None or (live is not None and live.recording)),
                "chats": [c for c in coach_store.chats(sid) if c["role"] in ("user", "coach")]}

    @app.get("/api/sessions/{sid}/replay", tags=["coach"])
    def session_replay(sid: int, request: Request):
        """Recorded traffic of a sandbox session (every 5 simulated seconds) for the 3D replay."""
        s = _session(sid, request)
        frames = coach_store.recording(sid)
        live = _live_coach(s)
        if frames is None and live is not None and live.recording:
            frames = list(live.recording)
        if not frames:
            raise Problem(404, "no_recording")
        return {"frames": frames}

    @app.post("/api/sessions/{sid}/debrief", tags=["coach"])
    def session_debrief(sid: int, req: DebriefRequest, request: Request):
        """The coach's debrief of the session, in your language (Claude when configured,
        otherwise built-in templates)."""
        s = _session(sid, request)
        entries = _session_data(s)
        role = "debrief:%s:%d" % (req.lang, len(entries))
        cached = next((c for c in coach_store.chats(sid) if c["role"] == role), None)
        if cached:
            return {"provider": "cache", "text": cached["text"], "messages": []}
        ex = exercises.BY_ID.get(s["exercise"]) if s.get("exercise") else None
        record = session_record(s, entries, ex.title if ex else None)
        out = coach.debrief(record, req.lang)
        if out.get("text"):
            coach_store.add_chat(sid, role, out["text"])
        return out

    @app.post("/api/sessions/{sid}/ask", tags=["coach"])
    def session_ask(sid: int, req: AskRequest, request: Request):
        """Ask the coach about this session. Answers are grounded on its graded situations."""
        s = _session(sid, request)
        username = request.state.user["username"]
        if coach_store.chats_today(username) >= config.COACH_DAILY_QUESTIONS:
            raise Problem(429, "coach_daily_limit")
        if username in asking:
            raise Problem(429, "coach_busy")
        asking.add(username)
        try:
            entries = _session_data(s)
            ex = exercises.BY_ID.get(s["exercise"]) if s.get("exercise") else None
            record = session_record(s, entries, ex.title if ex else None)
            chats = [c for c in coach_store.chats(sid) if c["role"] in ("user", "coach")]
            history = [(q["text"], a["text"]) for q, a in zip(chats[0::2], chats[1::2])]
            out = coach.answer(req.question, record, history, req.lang)
            if out.get("text"):
                coach_store.add_chat(sid, "user", req.question)
                coach_store.add_chat(sid, "coach", out["text"])
            return out
        finally:
            asking.discard(username)

    # ------------------------------------------------------------------ static UI
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(config.PUBLIC_DIR / "index.html")

    @app.get("/login", include_in_schema=False)
    def login_page():
        return FileResponse(config.PUBLIC_DIR / "login.html")

    @app.get("/admin", include_in_schema=False)
    def admin_page():
        return FileResponse(config.PUBLIC_DIR / "admin.html")

    app.mount("/", StaticFiles(directory=str(config.PUBLIC_DIR)), name="static")
    return app
