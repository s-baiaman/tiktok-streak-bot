import asyncio

import pytest
from fastapi.testclient import TestClient

from fakes import FakeTikTok
from streakbot.auth import LoginThrottle, SessionSigner, hash_password
from streakbot.crypto import CookieBox, load_or_create_key
from streakbot.db import Recipient, Repo
from streakbot.service import PanelService
from streakbot.tiktok.client import Profile
from streakbot.tiktok.errors import TikTokError
from streakbot.web.app import COOKIE_NAME, create_app
from streakbot.web.events import EventHub

PASSWORD = "hunter22hunter"
HASH = hash_password(PASSWORD)
SID = "abc123def456abc1"
H = {"X-Panel": "1"}


@pytest.fixture
def tiktok():
    return FakeTikTok()


@pytest.fixture
def svc(tmp_path, tiktok):
    return PanelService(Repo(tmp_path / "db.sqlite"), CookieBox(load_or_create_key(tmp_path / "k")),
                        tiktok, "Europe/Moscow")


@pytest.fixture
def signer():
    return SessionSigner(b"s" * 32, HASH)


@pytest.fixture
def anon(svc, signer):
    app = create_app(svc, EventHub(), signer, LoginThrottle(limit=3), HASH)
    return TestClient(app)


@pytest.fixture
def client(anon):
    r = anon.post("/api/login", json={"password": PASSWORD}, headers=H)
    assert r.status_code == 204
    return anon


def add_bot(client, **extra):
    r = client.post("/api/bots", json={"sessionid": SID, **extra}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()["bot"]


# --- auth -------------------------------------------------------------------------

def test_login_sets_strict_httponly_cookie(anon):
    r = anon.post("/api/login", json={"password": PASSWORD}, headers=H)
    assert r.status_code == 204
    cookie = r.headers["set-cookie"].lower()
    assert cookie.startswith(COOKIE_NAME)
    assert "httponly" in cookie and "samesite=strict" in cookie and "max-age=2592000" in cookie and "path=/" in cookie
    assert anon.get("/api/session").status_code == 204


def test_wrong_password(anon):
    r = anon.post("/api/login", json={"password": "nope"}, headers=H)
    assert r.status_code == 401 and r.json() == {"error": "Wrong password"}
    assert anon.get("/api/bots").status_code == 401


def test_login_throttled(anon):
    for _ in range(3):
        anon.post("/api/login", json={"password": "nope"}, headers=H)
    r = anon.post("/api/login", json={"password": PASSWORD}, headers=H)
    assert r.status_code == 429 and "error" in r.json()


async def test_concurrent_wrong_passwords_are_throttled(svc, signer):
    import httpx
    app = create_app(svc, EventHub(), signer, LoginThrottle(limit=3), HASH)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://panel") as c:
        rs = await asyncio.gather(*[c.post("/api/login", json={"password": "nope"}, headers=H) for _ in range(20)])
    codes = [r.status_code for r in rs]
    assert codes.count(401) <= 3 and codes.count(429) >= 17


def test_unauthenticated_and_forged(anon, signer):
    assert anon.get("/api/bots").json() == {"error": "Not logged in"}
    anon.cookies.set(COOKIE_NAME, "123.deadbeef")
    assert anon.get("/api/bots").status_code == 401
    assert anon.get("/api/events").status_code == 401


def test_cookie_from_old_password_rejected(anon):
    old = SessionSigner(b"s" * 32, hash_password("old password")).issue()
    anon.cookies.set(COOKIE_NAME, old)
    assert anon.get("/api/bots").status_code == 401


def test_mutation_needs_header(client):
    r = client.post("/api/bots", json={"sessionid": SID})
    assert r.status_code == 403
    assert client.post("/api/login", json={"password": PASSWORD}).status_code == 403


def test_logout(client):
    assert client.post("/api/logout", headers=H).status_code == 204
    assert client.get("/api/bots").status_code == 401


# --- bots ---------------------------------------------------------------------------

def test_add_and_list(client):
    bot = add_bot(client)
    assert bot["username"] == "owner.acct" and bot["message"] == "🔥" and bot["timezone"] == "Europe/Moscow"
    assert bot["status"] == {"level": "warn", "label": "No recipients"}
    assert bot["recipients"] == [] and bot["running"] is False and bot["next_send_at"] is None
    assert "cookies" not in str(bot) and SID not in str(bot)
    listed = client.get("/api/bots").json()["bots"]
    assert [b["id"] for b in listed] == [bot["id"]]
    r = client.post("/api/bots", json={"sessionid": SID}, headers=H)
    assert r.json()["outcome"] == "updated"


def test_add_bad_cookie(client, tiktok):
    tiktok.whoami_result = None
    r = client.post("/api/bots", json={"sessionid": SID}, headers=H)
    assert r.status_code == 422 and "cookie" in r.json()["error"]


def test_add_needs_region(client, tiktok):
    tiktok.region = None
    r = client.post("/api/bots", json={"sessionid": SID}, headers=H)
    assert r.status_code == 422 and r.json()["needs_region"] is True
    r = client.post("/api/bots", json={"sessionid": SID, "region": "alisg"}, headers=H)
    assert r.status_code == 200 and r.json()["bot"]["region"] == "alisg"


def test_add_garbage(client):
    r = client.post("/api/bots", json={"sessionid": "hello"}, headers=H)
    assert r.status_code == 422 and r.json()["error"]


def test_tiktok_unreachable_is_502(client, tiktok):
    tiktok.whoami_raises = TikTokError("boom")
    r = client.post("/api/bots", json={"sessionid": SID}, headers=H)
    assert r.status_code == 502 and r.json() == {"error": "Couldn't talk to TikTok. Try again."}


def test_invalid_body_is_json_error(client):
    r = client.post("/api/bots", json={"nope": 1}, headers=H)
    assert r.status_code == 422 and r.json() == {"error": "Invalid request."}


def test_update_login(client, tiktok):
    bot = add_bot(client)
    r = client.post(f"/api/bots/{bot['id']}/login", json={"sessionid": SID}, headers=H)
    assert r.status_code == 200 and r.json()["outcome"] == "updated"
    tiktok.whoami_result = "other.acct"
    r = client.post(f"/api/bots/{bot['id']}/login", json={"sessionid": SID}, headers=H)
    assert r.status_code == 422 and "@other.acct" in r.json()["error"]


def test_patch(client):
    bot = add_bot(client)
    r = client.patch(f"/api/bots/{bot['id']}", json={"message": "yo", "send_time": "8pm", "paused": True}, headers=H)
    assert r.status_code == 200
    assert (r.json()["message"], r.json()["send_time"], r.json()["paused"]) == ("yo", "20:00", True)
    r = client.patch(f"/api/bots/{bot['id']}", json={"send_time": "99:99"}, headers=H)
    assert r.status_code == 422 and r.json()["error"]


def test_recipients_put(client):
    bot = add_bot(client)
    r = client.put(f"/api/bots/{bot['id']}/recipients",
                   json={"recipients": [{"username": "@Sam.R_07", "display_name": "Sam"}]}, headers=H)
    assert r.status_code == 200
    assert r.json()["recipients"] == [{"username": "sam.r_07", "display_name": "Sam"}]


def test_delete(client):
    bot = add_bot(client)
    assert client.delete(f"/api/bots/{bot['id']}", headers=H).status_code == 204
    assert client.delete(f"/api/bots/{bot['id']}", headers=H).status_code == 404
    assert client.patch(f"/api/bots/{bot['id']}", json={"message": "x"}, headers=H).json() == {"error": "Bot not found"}


def test_chats_and_lookup(client, tiktok):
    bot = add_bot(client)
    tiktok.chats = [Profile("sam.r_07", "Sam", "https://img/x.jpg")]
    tiktok.profiles = {"kai.m": Profile("kai.m", "Kai", None)}
    r = client.get(f"/api/bots/{bot['id']}/chats")
    assert r.json() == {"chats": [{"username": "sam.r_07", "display_name": "Sam", "avatar_url": "https://img/x.jpg"}]}
    r = client.get(f"/api/bots/{bot['id']}/lookup", params={"username": "@kai.m"})
    assert r.json()["profile"]["display_name"] == "Kai"
    r = client.get(f"/api/bots/{bot['id']}/lookup", params={"username": "nobody.here"})
    assert r.status_code == 404 and "@nobody.here" in r.json()["error"]


def test_test_send(client, tiktok):
    bot = add_bot(client)
    r = client.post(f"/api/bots/{bot['id']}/test", json={"username": "kai.m"}, headers=H)
    assert r.json() == {"ok": True, "summary": "Sent 1/1"}
    assert tiktok.calls[-1] == ("send", ("kai.m",), "test", SID)
    tiktok.outcomes = {"kai.m": "not_found"}
    r = client.post(f"/api/bots/{bot['id']}/test", json={"username": "kai.m", "message": "hi"}, headers=H)
    assert r.json() == {"ok": False, "summary": "Sent 0/1 · failed: @kai.m (account not found)"}


def test_test_send_expired_login(client, tiktok):
    bot = add_bot(client)
    tiktok.expire = True
    r = client.post(f"/api/bots/{bot['id']}/test", json={"username": "kai.m"}, headers=H)
    assert r.status_code == 502 and "login expired" in r.json()["error"]


def test_send_now(client, svc):
    bot = add_bot(client)
    r = client.post(f"/api/bots/{bot['id']}/send-now", headers=H)
    assert r.status_code == 422 and r.json() == {"error": "Add at least one recipient first."}
    svc.set_recipients(bot["id"], [Recipient("sam.r_07", "Sam")])
    assert client.post(f"/api/bots/{bot['id']}/send-now", headers=H).status_code == 202
    assert client.get("/api/bots").json()["bots"][0]["last_run_summary"] == "Sent 1/1"


def test_busy_is_409(client, svc):
    bot = add_bot(client)
    svc._running.add(bot["id"])
    r = client.post(f"/api/bots/{bot['id']}/test", json={"username": "kai.m"}, headers=H)
    assert r.status_code == 409 and "busy" in r.json()["error"]
    assert client.get("/api/bots").json()["bots"][0]["status"] == {"level": "busy", "label": "Sending…"}


# --- events -------------------------------------------------------------------------

async def test_event_hub_wakes_subscribers():
    hub = EventHub()
    stream = hub.subscribe(ping_interval=5)
    first = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)
    hub.publish()
    hub.publish()  # coalesced
    assert await asyncio.wait_for(first, 1) == "changed"
    await stream.aclose()
    hub.publish()  # no subscribers: no error


async def test_event_hub_pings():
    hub = EventHub()
    stream = hub.subscribe(ping_interval=0.01)
    assert await asyncio.wait_for(anext(stream), 1) == "ping"
    await stream.aclose()


# --- page -------------------------------------------------------------------------

def test_index_served(anon):
    r = anon.get("/")
    assert r.status_code == 200 and 'id="app"' in r.text
    assert anon.get("/static/app.js").status_code == 200
    assert anon.get("/static/style.css").status_code == 200
    assert anon.get("/static/fire-emoji.woff2").status_code == 200
    logo = anon.get("/static/logo.png")
    assert logo.status_code == 200 and logo.headers["content-type"] == "image/png"
