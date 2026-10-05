"""Passwordless sign-in.

A sign-in link is emailed; its token is stored only as a SHA-256 hash, expires in
15 minutes and works once. Opening the link shows a confirm button rather than
signing in on GET, so email security scanners that prefetch links can't burn it.
"""
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from datetime import timedelta
from typing import Optional

from fastapi import Depends, HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlmodel import Session, func, select

from .config import settings
from .db import get_session
from .models import LoginToken, User, utcnow

SESSION_COOKIE = "lanyard_session"
ANON_COOKIE = "lanyard_anon"
SESSION_MAX_AGE = 60 * 60 * 24 * 30
TOKEN_TTL = timedelta(minutes=15)

_serializer = URLSafeTimedSerializer(settings.secret_key or "dev-insecure", salt="session")


class LoginRequired(Exception):
    def __init__(self, next_url: str):
        self.next_url = next_url


# ---- Rate limiting (per process; fine for a single app container) ----------
_ip_hits: dict[str, deque] = defaultdict(deque)


def ip_allowed(ip: str, limit: int = 10, window: int = 900) -> bool:
    now = time.monotonic()
    q = _ip_hits[ip]
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(now)
    return True


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_login_token(session: Session, email: str, next_url: str) -> Optional[str]:
    """Returns a raw token, or None if this address has asked too often."""
    window_start = utcnow() - timedelta(minutes=15)
    recent = session.exec(
        select(func.count()).select_from(LoginToken)
        .where(LoginToken.email == email, LoginToken.created_at > window_start)
    ).one()
    if recent >= 3:
        return None
    raw = secrets.token_urlsafe(32)
    session.add(LoginToken(token_hash=_hash(raw), email=email, next_url=next_url,
                           expires_at=utcnow() + TOKEN_TTL))
    session.commit()
    return raw


def peek_token(session: Session, raw: str) -> Optional[LoginToken]:
    tok = session.exec(select(LoginToken).where(LoginToken.token_hash == _hash(raw))).first()
    if not tok or tok.used_at or tok.expires_at < utcnow():
        return None
    return tok


def consume_token(session: Session, raw: str) -> Optional[User]:
    tok = peek_token(session, raw)
    if not tok:
        return None
    tok.used_at = utcnow()
    user = session.exec(select(User).where(User.email == tok.email)).first()
    if user is None:
        user = User(email=tok.email)
    if user.banned:
        session.commit()
        return None
    if tok.email in settings.admin_emails:
        user.is_admin = True
    session.add(user)
    session.add(tok)
    session.commit()
    session.refresh(user)
    return user


def session_cookie_value(user: User) -> str:
    return _serializer.dumps({"u": user.id, "v": user.session_version})


def set_session(response, user: User) -> None:
    response.set_cookie(SESSION_COOKIE, session_cookie_value(user), max_age=SESSION_MAX_AGE,
                        httponly=True, samesite="lax", secure=settings.secure_cookies)


def clear_session(response) -> None:
    response.delete_cookie(SESSION_COOKIE)


def get_current_user(request: Request, session: Session = Depends(get_session)) -> Optional[User]:
    if hasattr(request.state, "user"):
        return request.state.user
    user = None
    raw = request.cookies.get(SESSION_COOKIE)
    if raw:
        try:
            data = _serializer.loads(raw, max_age=SESSION_MAX_AGE)
            candidate = session.get(User, data.get("u"))
            if candidate and not candidate.banned and candidate.session_version == data.get("v"):
                user = candidate
        except BadSignature:
            pass
    request.state.user = user
    return user


def require_user(request: Request, user: Optional[User] = Depends(get_current_user)) -> User:
    if user is None:
        nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        if request.method != "GET":
            # Send them back to the page the form was on, not the POST endpoint.
            ref = request.headers.get("hx-current-url") or request.headers.get("referer") or "/"
            nxt = "/" + ref.split("/", 3)[-1] if ref.startswith(settings.origin) else "/"
        raise LoginRequired(nxt)
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(404)
    return user


# ---- CSRF ------------------------------------------------------------------
# Session cookies are SameSite=Lax, and every state-changing request must also
# carry a token bound to the session cookie (hidden field or X-CSRF-Token header)
# and, when the browser sends one, a matching Origin.

def csrf_token(request: Request) -> str:
    seed = (request.cookies.get(SESSION_COOKIE) or request.cookies.get(ANON_COOKIE)
            or getattr(request.state, "anon_seed", ""))
    key = (settings.secret_key or "dev-insecure").encode()
    return hmac.new(key, ("csrf:" + seed).encode(), hashlib.sha256).hexdigest()[:40]


async def csrf_protect(request: Request) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    origin = request.headers.get("origin")
    if origin and origin != settings.origin:
        raise HTTPException(403, "Cross-site request blocked.")
    sent = request.headers.get("x-csrf-token")
    if not sent:
        form = await request.form()
        sent = form.get("csrf_token")
    if not sent or not hmac.compare_digest(str(sent), csrf_token(request)):
        raise HTTPException(403, "This form expired. Reload the page and try again.")
