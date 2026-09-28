"""HTTP + WebSocket API. The browser UI and external AI agents use exactly the same endpoints.

Interactive documentation (OpenAPI) is served at /docs.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
import time
from typing import Any, Dict, List, Literal, Optional

from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, config, sectors
from .about import about as about_info, copyright_line
from .auth import AuthError, AuthStore, LoginThrottle
from .clearances import KINDS, Clearance
from .engine import Engine
from .navdata import NavData
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


class SectorRequest(BaseModel):
    sector: Optional[str] = None


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


COOKIE = "visor_session"
# Reachable without a session: the login page and what it needs, plus the health probe.
PUBLIC_EXACT = {"/login", "/login.html", "/api/health", "/api/auth/login", "/favicon.ico", "/js/login.js"}
PUBLIC_PREFIX = ("/css/",)
ADMIN_PATHS = {"/admin", "/admin.html"}
# Allowed while a password change is pending
CHANGE_ALLOWED = {"/api/auth/me", "/api/auth/password", "/api/auth/logout"}


def _is_api(path):
    return path.startswith("/api/") or path in ("/docs", "/redoc", "/openapi.json")


def _issuer(user, requested):
    """Humans are identified by their account; external agents may name themselves "ai:<name>"."""
    if requested and requested.startswith("ai:"):
        return requested
    return "human:" + user["username"]


def _public_user(u):
    return {k: u[k] for k in ("id", "username", "role", "disabled", "must_change",
                              "created", "updated", "last_login", "tutorial_state", "tutorial_step")}


# ---------------------------------------------------------------------------- app
def create_app():
    store = Store()
    navdata = NavData()
    recorder = Recorder(store)
    engine = Engine(store, navdata)
    auth = AuthStore()
    auth.bootstrap()
    throttle = LoginThrottle()
    tutorials = TutorialManager(navdata)
    sockets = {}            # WebSocket -> user id for training-sandbox subscribers, None for live

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

    async def _broadcast():
        last = {}               # ws -> last frame sent
        ticks = 0
        while True:
            await asyncio.sleep(config.TICK_REAL_S)
            ticks += 1
            if ticks % 120 == 0:
                tutorials.reap()
            if not sockets:
                continue
            sandboxes = tutorials.engines()
            for ws, uid in list(sockets.items()):
                eng = engine if uid is None else sandboxes.get(uid)
                if eng is None:                 # sandbox ended (stopped or reaped)
                    sockets.pop(ws, None)
                    try:
                        await ws.close(code=4404)
                    except Exception:
                        pass
                    continue
                frame = eng.frame
                if frame is None or frame is last.get(ws):
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
        public = path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIX)

        # Cookie-authenticated state changes must come from our own origin (CSRF defence).
        if (not bearer and request.method not in ("GET", "HEAD", "OPTIONS")
                and request.headers.get("origin")):
            origin_host = urlsplit(request.headers["origin"]).netloc
            host = request.headers.get("x-forwarded-host") or request.headers.get("host")
            if origin_host != host:
                return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)

        if public:
            return await call_next(request)
        if user is None:
            if _is_api(path):
                return JSONResponse({"detail": "authentication required"}, status_code=401)
            nxt = path + ("?" + request.url.query if request.url.query else "")
            return RedirectResponse("/login?next=" + quote(nxt, safe="/"), status_code=302)
        if user["must_change"] and path not in CHANGE_ALLOWED:
            if _is_api(path):
                return JSONResponse({"detail": "password change required"}, status_code=403)
            return RedirectResponse("/login?change=1", status_code=302)
        if (path in ADMIN_PATHS or path.startswith("/api/admin/")) and user["role"] != "admin":
            if _is_api(path):
                return JSONResponse({"detail": "admin only"}, status_code=403)
            return RedirectResponse("/", status_code=302)
        return await call_next(request)

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
            raise HTTPException(429, "too many failed attempts, try again in %d s" % wait)
        user = auth.authenticate(req.username, req.password)
        if user is None:
            throttle.failed(req.username, ip)
            raise HTTPException(401, "invalid username or password")
        throttle.succeeded(req.username)
        auth.purge_expired()
        token = auth.create_session(user["id"])
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

    @app.post("/api/auth/password", tags=["auth"])
    def change_password(req: PasswordChange, request: Request):
        user = request.state.user
        try:
            updated = auth.change_own_password(user["id"], req.current_password, req.new_password)
        except AuthError as exc:
            raise HTTPException(400, str(exc))
        # other sessions of this user are signed out; this one stays valid
        token = auth.create_session(user["id"])
        resp = JSONResponse({"user": _public_user(updated)})
        resp.set_cookie(COOKIE, token, max_age=int(config.SESSION_HOURS * 3600), httponly=True,
                        samesite="lax", secure=_cookie_secure(request), path="/")
        return resp

    def _engine(request):
        """The caller's private training sandbox (header X-ATC-Context: tutorial) or the shared sim."""
        if request.headers.get("x-atc-context") == "tutorial":
            box = tutorials.get(request.state.user["id"])
            if box is None:
                raise HTTPException(409, "no training session: start the tutorial first")
            return box.engine
        return engine

    # ------------------------------------------------------------------ guided tutorial
    @app.post("/api/tutorial/start", tags=["tutorial"])
    def tutorial_start(request: Request):
        """(Re)create the caller's private training sandbox. Use it with `X-ATC-Context: tutorial`."""
        user = request.state.user
        try:
            box = tutorials.start(user["id"])
        except OverflowError as exc:
            raise HTTPException(429, str(exc))
        return {"started": box.created, "sector": "ALPS-UPPER", "user": _public_user(auth.get_user(user["id"]))}

    @app.post("/api/tutorial/stop", tags=["tutorial"])
    def tutorial_stop(request: Request):
        return {"stopped": tutorials.stop(request.state.user["id"])}

    @app.post("/api/tutorial/scenario", tags=["tutorial"])
    def tutorial_scenario(req: TutorialScenario, request: Request):
        """Trigger a scripted situation in the sandbox (e.g. the head-on conflict lesson)."""
        box = tutorials.get(request.state.user["id"])
        if box is None:
            raise HTTPException(409, "no training session: start the tutorial first")
        return box.inject_conflict()

    @app.post("/api/tutorial/progress", tags=["tutorial"])
    def tutorial_progress(req: TutorialProgress, request: Request):
        return _public_user(auth.set_tutorial(request.state.user["id"], step=req.step))

    @app.post("/api/tutorial/complete", tags=["tutorial"])
    def tutorial_complete(request: Request):
        uid = request.state.user["id"]
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
            raise HTTPException(400, str(exc))

    @app.patch("/api/admin/users/{user_id}", tags=["admin"])
    def update_user(user_id: int, req: UserUpdate, request: Request):
        try:
            return _public_user(auth.update_user(
                user_id, actor_id=request.state.user["id"], role=req.role, password=req.password,
                disabled=req.disabled, must_change=req.must_change, tutorial_reset=req.tutorial_reset))
        except AuthError as exc:
            raise HTTPException(404 if str(exc) == "no such user" else 400, str(exc))

    @app.delete("/api/admin/users/{user_id}", tags=["admin"])
    def delete_user(user_id: int, request: Request):
        try:
            auth.delete_user(user_id, actor_id=request.state.user["id"])
        except AuthError as exc:
            raise HTTPException(404 if str(exc) == "no such user" else 400, str(exc))
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
        uid = None
        src = engine
        if ws.query_params.get("ctx") == "tutorial":
            box = tutorials.get(user["id"])
            if box is None:
                await ws.close(code=4404)
                return
            uid, src = user["id"], box.engine
        sockets[ws] = uid
        try:
            if src.frame:
                await ws.send_text(src.frame)
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
                "recorder": recorder.status(), "ai": eng.ai_status(),
                "sim": {"t": eng.t, "mode": eng.mode, "speed": eng.speed,
                        "paused": eng.paused, "lockstep": eng.lockstep,
                        "aircraft": len(eng.aircraft)},
                "area": config.EUROPE, "now": time.time()}

    @app.get("/api/navdata", tags=["info"])
    def get_navdata():
        return {"fixes": [[f["ident"], f["kind"], round(f["lat"], 4), round(f["lon"], 4), f["name"]]
                          for f in navdata.fixes]}

    @app.get("/api/sectors", tags=["info"])
    def get_sectors():
        return sectors.SECTORS

    # ------------------------------------------------------------------ observation
    @app.get("/api/observation", tags=["agent"])
    def observation(request: Request, sector_only: bool = False):
        """Complete typed state: aircraft, conflicts, open decision points, score."""
        eng = _engine(request)
        return eng.observation(sector_only)

    @app.get("/api/aircraft/{ident}", tags=["agent"])
    def aircraft(ident: str, request: Request):
        eng = _engine(request)
        d = eng.aircraft_detail(ident)
        if d is None:
            raise HTTPException(404, "no such aircraft")
        return d

    @app.get("/api/events", tags=["agent"])
    def events(request: Request, since: int = 0):
        eng = _engine(request)
        return eng.events_since(since)

    # ------------------------------------------------------------------ actions
    @app.post("/api/clearance", tags=["agent"])
    def clearance(req: ClearanceRequest, request: Request):
        eng = _engine(request)
        try:
            clrs = [Clearance(c.kind, c.value, c.direction) for c in req.clearances]
            return eng.issue(req.aircraft, clrs, _issuer(request.state.user, req.issuer))
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/command", tags=["agent"])
    def command(req: CommandRequest, request: Request):
        """ATC shorthand, e.g. `DLH4AB C 370`, `EZY12 TL 270`, `RYR1 DCT KPT`, `AFR7 RON`."""
        eng = _engine(request)
        try:
            return eng.command(req.text, _issuer(request.state.user, req.issuer))
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.get("/api/decisions", tags=["agent"])
    def list_decisions(request: Request, status: Optional[str] = "open"):
        eng = _engine(request)
        with eng.lock:
            return [dp.to_dict() for dp in eng.decisions.values()
                    if status is None or dp.status == status]

    @app.post("/api/decisions/{dp_id}", tags=["agent"])
    def answer(dp_id: str, req: DecisionAnswer, request: Request):
        eng = _engine(request)
        try:
            return eng.answer_decision(dp_id, req.answers, _issuer(request.state.user, req.by))
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/decisions/{dp_id}/dismiss", tags=["agent"])
    def dismiss(dp_id: str, request: Request):
        eng = _engine(request)
        eng.dismiss_decision(dp_id)
        return {"ok": True}

    # ------------------------------------------------------------------ control
    @app.post("/api/sim", tags=["control"])
    def sim(req: SimControl, request: Request):
        eng = _engine(request)
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
                    raise ValueError("enable lockstep first")
                eng.step(max(0.1, min(600.0, req.dt or 5.0)))
                with eng.lock:
                    eng._build_frame()
                return eng.observation()
            elif req.action == "reset":
                if eng is not engine:
                    raise ValueError("restart the training scenario with POST /api/tutorial/start")
                start = req.start
                if start is None and req.hours_ago is not None:
                    start = time.time() - req.hours_ago * 3600
                eng.reset(req.mode or "live", start)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return status(request)

    @app.post("/api/sector", tags=["control"])
    def set_sector(req: SectorRequest, request: Request):
        eng = _engine(request)
        if req.sector and req.sector not in sectors.BY_ID:
            raise HTTPException(400, "unknown sector")
        eng.set_sector(req.sector)
        return {"sector": req.sector}

    @app.post("/api/ai", tags=["control"])
    def set_ai(req: AiRequest, request: Request):
        eng = _engine(request)
        try:
            return eng.set_ai(req.mode, req.agent)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

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
        }

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
