"""FastAPI app: JSON API, live event stream and the dashboard page."""
from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..auth import LoginThrottle, SessionSigner, verify_password
from ..db import Bot, Recipient
from ..parsing import ParseError, normalize_username
from ..scheduling import next_send_at
from ..service import AddOutcome, BotNotFound, Busy, NoRecipients, PanelService, summarize
from ..tiktok.errors import SessionExpired, TikTokError
from .events import EventHub

log = logging.getLogger(__name__)

COOKIE_NAME = "panel_session"
STATIC_DIR = Path(__file__).parent / "static"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
COOKIE_FLAGS = {"path": "/", "httponly": True, "samesite": "strict"}

# Domain errors → (HTTP status, message shown on the page). SessionExpired is listed before its
# parent TikTokError; Starlette picks the most specific handler either way.
ERROR_RESPONSES: dict[type[Exception], tuple[int, str | None]] = {
    ParseError: (422, None),  # None: the exception's own message is the user-facing text
    BotNotFound: (404, "Bot not found"),
    Busy: (409, "This bot is busy sending. Try again in a minute."),
    NoRecipients: (422, "Add at least one recipient first."),
    SessionExpired: (502, "This bot's TikTok login expired. Update its login."),
    TikTokError: (502, "Couldn't talk to TikTok. Try again."),
}


class ApiError(Exception):
    def __init__(self, status: int, message: str, **extra):
        self.status = status
        self.body = {"error": message, **extra}


class LoginBody(BaseModel):
    password: str = Field(max_length=1000)


class CookieBody(BaseModel):
    sessionid: str = Field(max_length=20000)
    region: str | None = Field(default=None, max_length=50)


class PatchBody(BaseModel):
    message: str | None = Field(default=None, max_length=1000)
    send_time: str | None = Field(default=None, max_length=20)
    timezone: str | None = Field(default=None, max_length=60)
    paused: bool | None = None


class RecipientIn(BaseModel):
    username: str = Field(max_length=200)
    display_name: str = Field(default="", max_length=200)


class RecipientsBody(BaseModel):
    recipients: list[RecipientIn] = Field(max_length=100)


class TestBody(BaseModel):
    username: str = Field(max_length=200)
    message: str | None = Field(default=None, max_length=1000)


def create_app(
    service: PanelService,
    hub: EventHub,
    signer: SessionSigner,
    throttle: LoginThrottle,
    password_hash: str,
    static_dir: Path = STATIC_DIR,
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    # --- errors ----------------------------------------------------------------------

    def _json(status: int, body: dict) -> JSONResponse:
        return JSONResponse(body, status_code=status)

    @app.exception_handler(ApiError)
    async def _api_error(_, e: ApiError):
        return _json(e.status, e.body)

    @app.exception_handler(RequestValidationError)
    async def _invalid(_, e):
        return _json(422, {"error": "Invalid request."})

    async def _domain_error(_, e: Exception):
        status, message = next(v for cls, v in ERROR_RESPONSES.items() if isinstance(e, cls))
        if isinstance(e, TikTokError) and not isinstance(e, SessionExpired):
            log.warning("TikTok error during a request (%s)", type(e).__name__)
        return _json(status, {"error": message or str(e)})

    for exc_class in ERROR_RESPONSES:
        app.add_exception_handler(exc_class, _domain_error)

    # --- guards ------------------------------------------------------------------------

    @app.middleware("http")
    async def _csrf(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.method not in SAFE_METHODS \
                and request.headers.get("x-panel") != "1":
            return _json(403, {"error": "Missing X-Panel header."})
        return await call_next(request)

    def require_session(request: Request) -> None:
        if not signer.valid(request.cookies.get(COOKIE_NAME)):
            raise ApiError(401, "Not logged in")

    authed = [Depends(require_session)]

    def bot_json(bot: Bot) -> dict:
        now = datetime.now(UTC)
        recipients = service.recipients(bot.id)
        level, label = service.status(bot, now, recipients)
        nxt = next_send_at(bot, now)
        return {
            "id": bot.id,
            "username": bot.tiktok_username,
            "region": bot.region,
            "message": bot.message,
            "send_time": bot.send_time,
            "timezone": bot.timezone,
            "paused": bot.paused,
            "recipients": [asdict(r) for r in recipients],
            "status": {"level": level, "label": label},
            "running": service.is_running(bot.id),
            "last_run_at": bot.last_run_at,
            "last_run_summary": bot.last_run_summary,
            "next_send_at": nxt.isoformat() if nxt else None,
            "session_expires_at": bot.session_expires_at,
        }

    def login_result(outcome: AddOutcome, bot: Bot | None) -> dict:
        if outcome is AddOutcome.BAD_COOKIE:
            raise ApiError(422, "TikTok didn't accept that cookie. Copy a fresh sessionid and try again.")
        if outcome is AddOutcome.NEEDS_REGION:
            raise ApiError(
                422,
                "Logged in, but TikTok needs this account's region cookie too. "
                "Paste the tt-target-idc value from the same browser.",
                needs_region=True,
            )
        return {"outcome": outcome.value, "bot": bot_json(bot)}

    # --- session -------------------------------------------------------------------------

    @app.post("/api/login", status_code=204)
    async def login(body: LoginBody, request: Request):
        client = request.client.host if request.client else "unknown"
        if not throttle.allowed(client):
            raise ApiError(429, "Too many wrong passwords. Wait 15 minutes and try again.")
        # Count the attempt before the slow check, so parallel guesses can't all slip past the limit.
        throttle.failed(client)
        if not await run_in_threadpool(verify_password, body.password, password_hash):
            log.warning("failed panel login")
            raise ApiError(401, "Wrong password")
        throttle.reset(client)
        response = Response(status_code=204)
        response.set_cookie(COOKIE_NAME, signer.issue(), max_age=SessionSigner.MAX_AGE, **COOKIE_FLAGS)
        return response

    @app.post("/api/logout", status_code=204)
    async def logout():
        response = Response(status_code=204)
        response.delete_cookie(COOKIE_NAME, **COOKIE_FLAGS)
        return response

    @app.get("/api/session", status_code=204, dependencies=authed)
    async def session():
        return Response(status_code=204)

    # --- bots --------------------------------------------------------------------------------

    @app.get("/api/bots", dependencies=authed)
    async def list_bots():
        return {"bots": [bot_json(b) for b in service.list_bots()]}

    @app.post("/api/bots", dependencies=authed)
    async def add_bot(body: CookieBody):
        return login_result(*await service.add_bot(body.sessionid, body.region))

    @app.post("/api/bots/{bot_id}/login", dependencies=authed)
    async def update_login(bot_id: int, body: CookieBody):
        return login_result(*await service.update_login(bot_id, body.sessionid, body.region))

    @app.patch("/api/bots/{bot_id}", dependencies=authed)
    async def patch_bot(bot_id: int, body: PatchBody):
        return bot_json(service.update_settings(
            bot_id, message=body.message, send_time=body.send_time, timezone=body.timezone, paused=body.paused,
        ))

    @app.put("/api/bots/{bot_id}/recipients", dependencies=authed)
    async def put_recipients(bot_id: int, body: RecipientsBody):
        service.set_recipients(bot_id, [Recipient(**r.model_dump()) for r in body.recipients])
        return bot_json(service.get_bot(bot_id))

    @app.delete("/api/bots/{bot_id}", status_code=204, dependencies=authed)
    async def delete_bot(bot_id: int):
        service.delete_bot(bot_id)
        return Response(status_code=204)

    @app.get("/api/bots/{bot_id}/chats", dependencies=authed)
    async def chats(bot_id: int):
        return {"chats": [asdict(p) for p in await service.recent_chats(bot_id)]}

    @app.get("/api/bots/{bot_id}/lookup", dependencies=authed)
    async def lookup(bot_id: int, username: str):
        profile = await service.lookup(bot_id, username)
        if profile is None:
            raise ApiError(404, f"No TikTok account called @{normalize_username(username)}.")
        return {"profile": asdict(profile)}

    @app.post("/api/bots/{bot_id}/test", dependencies=authed)
    async def test_send(bot_id: int, body: TestBody):
        result = await service.test_send(bot_id, body.username, body.message)
        return {"ok": result.ok, "summary": summarize([result])}

    async def _send_now_in_background(bot_id: int) -> None:
        try:
            await service.send_now(bot_id)
        except (Busy, NoRecipients, BotNotFound):
            pass  # state changed between the request's check and the background start
        except Exception as e:
            log.error("send now failed for bot id %s (%s)", bot_id, type(e).__name__)

    @app.post("/api/bots/{bot_id}/send-now", status_code=202, dependencies=authed)
    async def send_now(bot_id: int, background: BackgroundTasks):
        service.ensure_can_send(bot_id)  # fail the request now, not silently in the background
        background.add_task(_send_now_in_background, bot_id)
        return Response(status_code=202)

    # --- live events ---------------------------------------------------------------------------

    @app.get("/api/events", dependencies=authed)
    async def events():
        async def stream():
            yield "retry: 3000\n\n"
            async for kind in hub.subscribe():
                yield "event: changed\ndata: {}\n\n" if kind == "changed" else ": ping\n\n"

        return StreamingResponse(
            stream(), media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    # --- page ------------------------------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(static_dir / "index.html", headers={"Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    return app
