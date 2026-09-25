"""HTTP + WebSocket API. The browser UI and external AI agents use exactly the same endpoints.

Interactive documentation (OpenAPI) is served at /docs.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
import time
from typing import Any, Dict, List, Literal, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, config, sectors
from .clearances import KINDS, Clearance
from .engine import Engine
from .navdata import NavData
from .recorder import Recorder
from .store import Store

log = logging.getLogger("visor.api")


# ---------------------------------------------------------------------------- request models
class ClearanceIn(BaseModel):
    kind: Literal["CLIMB", "DESCEND", "LEVEL", "HEADING", "TURN", "DIRECT", "SPEED", "RESUME"]
    value: Optional[Any] = Field(None, description="FL, heading, degrees, fix ident or IAS (kt)")
    direction: Optional[Literal["L", "R"]] = None


class ClearanceRequest(BaseModel):
    aircraft: str = Field(..., description="Callsign or ICAO24 address")
    clearances: List[ClearanceIn]
    issuer: str = Field("human", description='"human" or "ai:<agent name>"')


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


# ---------------------------------------------------------------------------- app
def create_app():
    store = Store()
    navdata = NavData()
    recorder = Recorder(store)
    engine = Engine(store, navdata)
    sockets = set()

    @asynccontextmanager
    async def lifespan(_app):
        if config.RECORD:
            recorder.start()
        engine.start()
        task = asyncio.get_event_loop().create_task(_broadcast())
        yield
        task.cancel()
        engine.stop()
        recorder.stop()

    app = FastAPI(title="Visor ATC", version=__version__, lifespan=lifespan,
                  description="Air traffic control simulator on recorded OpenSky data.")
    app.state.engine = engine

    async def _broadcast():
        last = None
        while True:
            await asyncio.sleep(config.TICK_REAL_S)
            frame = engine.frame
            if frame is None or frame is last or not sockets:
                continue
            last = frame
            for ws in list(sockets):
                try:
                    await ws.send_text(frame)
                except Exception:
                    sockets.discard(ws)

    # ------------------------------------------------------------------ realtime
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        sockets.add(ws)
        try:
            if engine.frame:
                await ws.send_text(engine.frame)
            while True:
                await ws.receive_text()      # client messages are ignored; use REST for actions
        except WebSocketDisconnect:
            pass
        finally:
            sockets.discard(ws)

    # ------------------------------------------------------------------ status & static data
    @app.get("/api/status", tags=["info"])
    def status():
        return {"version": __version__, "recording": config.RECORD,
                "recorder": recorder.status(), "ai": engine.ai_status(),
                "sim": {"t": engine.t, "mode": engine.mode, "speed": engine.speed,
                        "paused": engine.paused, "lockstep": engine.lockstep,
                        "aircraft": len(engine.aircraft)},
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
    def observation(sector_only: bool = False):
        """Complete typed state: aircraft, conflicts, open decision points, score."""
        return engine.observation(sector_only)

    @app.get("/api/aircraft/{ident}", tags=["agent"])
    def aircraft(ident: str):
        d = engine.aircraft_detail(ident)
        if d is None:
            raise HTTPException(404, "no such aircraft")
        return d

    @app.get("/api/events", tags=["agent"])
    def events(since: int = 0):
        return engine.events_since(since)

    # ------------------------------------------------------------------ actions
    @app.post("/api/clearance", tags=["agent"])
    def clearance(req: ClearanceRequest):
        try:
            clrs = [Clearance(c.kind, c.value, c.direction) for c in req.clearances]
            return engine.issue(req.aircraft, clrs, req.issuer)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/command", tags=["agent"])
    def command(req: CommandRequest):
        """ATC shorthand, e.g. `DLH4AB C 370`, `EZY12 TL 270`, `RYR1 DCT KPT`, `AFR7 RON`."""
        try:
            return engine.command(req.text, req.issuer)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.get("/api/decisions", tags=["agent"])
    def list_decisions(status: Optional[str] = "open"):
        with engine.lock:
            return [dp.to_dict() for dp in engine.decisions.values()
                    if status is None or dp.status == status]

    @app.post("/api/decisions/{dp_id}", tags=["agent"])
    def answer(dp_id: str, req: DecisionAnswer):
        try:
            return engine.answer_decision(dp_id, req.answers, req.by)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/decisions/{dp_id}/dismiss", tags=["agent"])
    def dismiss(dp_id: str):
        engine.dismiss_decision(dp_id)
        return {"ok": True}

    # ------------------------------------------------------------------ control
    @app.post("/api/sim", tags=["control"])
    def sim(req: SimControl):
        try:
            if req.action == "pause":
                engine.set_paused(True)
            elif req.action == "resume":
                engine.set_paused(False)
            elif req.action == "speed":
                engine.set_speed(req.speed or 1.0)
            elif req.action == "lockstep":
                engine.set_lockstep(bool(req.lockstep))
            elif req.action == "step":
                if not engine.lockstep:
                    raise ValueError("enable lockstep first")
                engine.step(max(0.1, min(600.0, req.dt or 5.0)))
                with engine.lock:
                    engine._build_frame()
                return engine.observation()
            elif req.action == "reset":
                start = req.start
                if start is None and req.hours_ago is not None:
                    start = time.time() - req.hours_ago * 3600
                engine.reset(req.mode or "live", start)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return status()

    @app.post("/api/sector", tags=["control"])
    def set_sector(req: SectorRequest):
        if req.sector and req.sector not in sectors.BY_ID:
            raise HTTPException(400, "unknown sector")
        engine.set_sector(req.sector)
        return {"sector": req.sector}

    @app.post("/api/ai", tags=["control"])
    def set_ai(req: AiRequest):
        try:
            return engine.set_ai(req.mode, req.agent)
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

    app.mount("/", StaticFiles(directory=str(config.PUBLIC_DIR)), name="static")
    return app
