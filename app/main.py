import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import jobs
from .auth import ANON_COOKIE, SESSION_COOKIE, LoginRequired, get_current_user
from .config import settings
from .db import init_db
from .routes import account, admin, engage, manage, public, registration
from .web import render

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("lanyard")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.secret_key:
        log.warning("SECRET_KEY is not set; sessions use an insecure development key.")
    # Refuse to start with values copied unchanged from .env.sample.
    import os
    unfilled = sorted(k for k, v in os.environ.items() if "CHANGE_ME" in v)
    if unfilled:
        raise RuntimeError(f"Replace the CHANGE_ME placeholder in: {', '.join(unfilled)}")
    init_db()
    task = asyncio.create_task(jobs.run_forever()) if settings.run_worker else None
    yield
    if task:
        task.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

app.include_router(manage.payments_router)  # before /manage/{event_id}
for r in (public, account, registration, engage, manage, admin):
    app.include_router(r.router)
app.include_router(registration.webhook_router)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    # Anonymous visitors get a random cookie so login forms have a CSRF seed.
    new_anon = None
    if not request.cookies.get(SESSION_COOKIE) and not request.cookies.get(ANON_COOKIE):
        new_anon = secrets.token_urlsafe(16)
        request.state.anon_seed = new_anon
    response = await call_next(request)
    if new_anon:
        response.set_cookie(ANON_COOKIE, new_anon, httponly=True, samesite="lax",
                            secure=settings.secure_cookies, max_age=60 * 60 * 24 * 7)
    h = response.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Content-Security-Policy",
                 "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                 "script-src 'self'; frame-ancestors 'none'; form-action 'self' https://checkout.stripe.com https://connect.stripe.com; "
                 "base-uri 'self'")
    return response


@app.exception_handler(LoginRequired)
async def login_required(request: Request, exc: LoginRequired):
    from urllib.parse import quote
    url = f"/login?next={quote(exc.next_url)}"
    if request.headers.get("hx-request") == "true":
        return PlainTextResponse("", status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if request.url.path.startswith("/stripe/"):
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code)
    try:
        from .db import engine
        from sqlmodel import Session
        with Session(engine) as s:
            get_current_user(request, s)
    except Exception:  # noqa: BLE001
        request.state.user = None
    titles = {404: "Page not found", 403: "Not allowed"}
    detail = exc.detail if exc.detail and exc.detail not in ("Not Found", "Forbidden") else None
    if request.headers.get("hx-request") == "true":
        return PlainTextResponse(detail or titles.get(exc.status_code, "Something went wrong"),
                                 status_code=exc.status_code)
    return render(request, "error.html", status_code=exc.status_code,
                  title=titles.get(exc.status_code, "Something went wrong"), detail=detail)


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}
