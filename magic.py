"""Magic word gate. Off unless MAGIC_WORD is set, so local runs stay open.

One shared word, no accounts: say it once, get a signed cookie for 30 days.
The cookie value is an HMAC of the word, so changing MAGIC_WORD logs
everyone out.
"""
import hashlib
import hmac
import os
import time
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

COOKIE = "ouioui_magic"
MAX_AGE = 30 * 24 * 3600
MAX_FAILS = 5            # per client, within WINDOW
WINDOW = 15 * 60         # seconds; also how long a locked-out client waits
OPEN_PATHS = ("/magic", "/static/")

# ponytail: in-memory per-IP failure log, resets on restart and is per process.
# Fine for one uvicorn worker; move to the DB if this ever runs replicated.
_fails: dict[str, list[float]] = {}


def _word() -> str:
    return os.environ.get("MAGIC_WORD", "")


def _token(word: str) -> str:
    return hmac.new(word.encode(), b"ouioui-magic-v1", hashlib.sha256).hexdigest()


def _client(request: Request) -> str:
    # Behind Cloudflare Tunnel every request arrives from cloudflared, so the
    # real visitor is in CF-Connecting-IP. The app is not reachable around it.
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")


def _recent_fails(client: str) -> list[float]:
    now = time.monotonic()
    fails = [t for t in _fails.get(client, []) if now - t < WINDOW]
    _fails[client] = fails
    return fails


def _authed(request: Request, word: str) -> bool:
    return hmac.compare_digest(request.cookies.get(COOKIE, ""), _token(word))


def install(app: FastAPI, templates: Jinja2Templates) -> None:
    @app.middleware("http")
    async def gate(request: Request, call_next):
        word = _word()
        path = request.url.path
        if not word or path.startswith(OPEN_PATHS) or _authed(request, word):
            return await call_next(request)
        target = "/magic?next=" + quote(path)
        if request.headers.get("hx-request"):
            # HTMX would swap a redirect's HTML into the page; make it navigate.
            return Response(status_code=401, headers={"HX-Redirect": target})
        return RedirectResponse(target, status_code=303)

    @app.get("/magic", response_class=HTMLResponse)
    def magic_page(request: Request, next: str = "/"):
        return templates.TemplateResponse(request, "magic.html", {"next": next, "error": None})

    @app.post("/magic", response_class=HTMLResponse)
    def magic_submit(request: Request, word: str = Form(""), next: str = Form("/")):
        expected = _word()
        client = _client(request)
        if not next.startswith("/") or next.startswith("//"):
            next = "/"  # no open redirect to another site
        if len(_recent_fails(client)) >= MAX_FAILS:
            return templates.TemplateResponse(
                request, "magic.html",
                {"next": next, "error": "Trop d'essais. Reviens dans 15 minutes."},
                status_code=429,
            )
        if expected and hmac.compare_digest(word.strip().encode(), expected.encode()):
            _fails.pop(client, None)
            resp = RedirectResponse(next, status_code=303)
            resp.set_cookie(COOKIE, _token(expected), max_age=MAX_AGE,
                            httponly=True, secure=True, samesite="lax")
            return resp
        _fails.setdefault(client, []).append(time.monotonic())
        time.sleep(1)  # slows guessing even before the lockout kicks in
        return templates.TemplateResponse(
            request, "magic.html",
            {"next": next, "error": "Ce n'est pas le mot magique."},
            status_code=401,
        )
