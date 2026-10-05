import re
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from .. import auth, mailer
from ..config import settings
from ..db import get_session
from ..models import Event, Registration, TicketType, User, utcnow
from ..util import safe_next
from ..web import render

router = APIRouter(dependencies=[Depends(auth.csrf_protect)])
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")


@router.get("/login")
def login_form(request: Request, next: str = "/", user: Optional[User] = Depends(auth.get_current_user)):
    if user:
        return RedirectResponse(safe_next(next), status_code=303)
    return render(request, "login.html", next=safe_next(next), error=None, email="")


@router.post("/login")
def login_send(request: Request, email: str = Form(""), next: str = Form("/"),
               session: Session = Depends(get_session)):
    email = email.strip().lower()
    nxt = safe_next(next)
    if not EMAIL_RE.match(email) or len(email) > 254:
        return render(request, "login.html", status_code=422, next=nxt, email=email,
                      error="Enter an email address like you@example.com.")
    if not auth.ip_allowed(auth.client_ip(request)):
        return render(request, "login.html", status_code=429, next=nxt, email=email,
                      error="Too many sign-in requests from this network. Wait 15 minutes and try again.")
    token = auth.create_login_token(session, email, nxt)
    if token:
        mailer.queue(session, email, "login.txt", kind="login", link=f"{settings.base_url}/auth/{token}")
        session.commit()
        from .. import jobs
        try:
            jobs.drain_outbox(batch=5)  # send right away rather than waiting for the next tick
        except Exception:  # noqa: BLE001
            pass
    # Same response either way, so the form can't be used to probe for accounts.
    return render(request, "login_sent.html", email=email)


@router.get("/auth/{token}")
def auth_confirm(token: str, request: Request, session: Session = Depends(get_session)):
    tok = auth.peek_token(session, token)
    return render(request, "login_confirm.html", token=token, valid=tok is not None,
                  email=tok.email if tok else "")


@router.post("/auth/{token}")
def auth_consume(token: str, request: Request, session: Session = Depends(get_session)):
    tok = auth.peek_token(session, token)
    nxt = safe_next(tok.next_url) if tok else "/"
    user = auth.consume_token(session, token)
    if not user:
        return render(request, "login_confirm.html", status_code=400, token=token, valid=False, email="")
    if not user.display_name:
        nxt = f"/me/welcome?next={nxt}"
    resp = RedirectResponse(nxt, status_code=303)
    auth.set_session(resp, user)
    return resp


@router.post("/logout")
def logout(request: Request):
    resp = RedirectResponse("/", status_code=303)
    auth.clear_session(resp)
    return resp


@router.get("/me/welcome")
def welcome(request: Request, next: str = "/", user: User = Depends(auth.require_user)):
    return render(request, "welcome.html", next=safe_next(next))


@router.post("/me/name")
def set_name(request: Request, display_name: str = Form(""), next: str = Form("/me"),
             session: Session = Depends(get_session), user: User = Depends(auth.require_user)):
    user.display_name = display_name.strip()[:60]
    session.add(user)
    session.commit()
    return RedirectResponse(safe_next(next), status_code=303)


@router.get("/me")
def me(request: Request, session: Session = Depends(get_session), user: User = Depends(auth.require_user)):
    rows = session.exec(
        select(Registration, Event, TicketType)
        .join(Event, Event.id == Registration.event_id)
        .join(TicketType, TicketType.id == Registration.ticket_type_id)
        .where(Registration.user_id == user.id, Registration.status.in_(("pending", "confirmed")))
        .order_by(Event.start_at)).all()
    now = utcnow()
    upcoming = [r for r in rows if r[1].end_at >= now and r[1].status != "cancelled"]
    past = [r for r in rows if r not in upcoming]
    hosting = session.exec(select(Event).where(Event.owner_id == user.id, Event.end_at >= now)
                           .order_by(Event.start_at)).all()
    return render(request, "me.html", upcoming=upcoming, past=past, hosting=hosting)


@router.post("/me/delete")
def delete_me(request: Request, confirm: str = Form(""), session: Session = Depends(get_session),
              user: User = Depends(auth.require_user)):
    from .. import services
    blockers = services.deletion_blockers(session, user)
    if blockers or confirm.strip().lower() != user.email.lower():
        return render(request, "delete_account.html", status_code=422, blockers=blockers,
                      error=None if blockers else "Type your email address exactly to confirm.")
    services.delete_account(session, user)
    resp = RedirectResponse("/?msg=deleted", status_code=303)
    auth.clear_session(resp)
    return resp


@router.get("/me/delete")
def delete_me_form(request: Request, session: Session = Depends(get_session),
                   user: User = Depends(auth.require_user)):
    from .. import services
    return render(request, "delete_account.html", blockers=services.deletion_blockers(session, user), error=None)


@router.post("/me/signout-everywhere")
def signout_everywhere(session: Session = Depends(get_session), user: User = Depends(auth.require_user)):
    user.session_version += 1
    session.add(user)
    session.commit()
    resp = RedirectResponse("/", status_code=303)
    auth.clear_session(resp)
    return resp
