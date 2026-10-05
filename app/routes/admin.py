"""Site admin: approve large events, take down events, ban accounts, watch email health.
Admins are whoever is listed in ADMIN_EMAILS."""
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlmodel import Session, func, select

from .. import auth, services
from ..config import settings
from ..db import get_session
from ..models import EmailOutbox, Event, User, utcnow
from ..web import redirect, render

router = APIRouter(prefix="/admin", dependencies=[Depends(auth.csrf_protect)])


@router.get("")
def admin_home(request: Request, q: str = "", session: Session = Depends(get_session),
               admin: User = Depends(auth.require_admin)):
    pending = [e for e in session.exec(select(Event).where(Event.status == "draft",
               Event.capacity > settings.free_capacity_limit, Event.large_event_approved == False)  # noqa: E712
               .order_by(Event.created_at.desc()).limit(50)).all()] if settings.free_capacity_limit else []
    recent = session.exec(select(Event).order_by(Event.created_at.desc()).limit(30)).all()
    owners = {u.id: u for u in session.exec(select(User).where(User.id.in_(
        [e.owner_id for e in recent + pending] or [0])))}
    users = []
    if q.strip():
        users = session.exec(select(User).where(User.email.ilike(f"%{q.strip()}%")).limit(25)).all()
    failed = session.exec(select(EmailOutbox).where(EmailOutbox.sent_at.is_(None), EmailOutbox.attempts > 0)
                          .order_by(EmailOutbox.id.desc()).limit(20)).all()
    stats = {
        "users": session.exec(select(func.count()).select_from(User)).one(),
        "live": session.exec(select(func.count()).select_from(Event).where(
            Event.status == "published", Event.end_at > utcnow())).one(),
        "queued": session.exec(select(func.count()).select_from(EmailOutbox).where(
            EmailOutbox.sent_at.is_(None), EmailOutbox.attempts < 5)).one(),
    }
    return render(request, "admin.html", pending=pending, recent=recent, owners=owners, users=users, q=q,
                  failed=failed, stats=stats, msg=request.query_params.get("msg"))


@router.post("/events/{event_id}/{action}")
def event_action(event_id: int, action: str, request: Request, session: Session = Depends(get_session),
                 admin: User = Depends(auth.require_admin)):
    ev = session.get(Event, event_id)
    if not ev:
        raise HTTPException(404)
    if action == "approve":
        ev.large_event_approved = True
        msg = f"Approved {ev.title}. The organizer can publish it now."
    elif action == "takedown":
        ev.status = "draft"
        ev.visibility = "unlisted"
        msg = f"Took down {ev.title}. It's a hidden draft now."
    else:
        raise HTTPException(404)
    session.add(ev)
    session.commit()
    return redirect(request, f"/admin?msg={quote(msg)}")


@router.post("/users/{user_id}/ban")
def ban(user_id: int, request: Request, session: Session = Depends(get_session),
        admin: User = Depends(auth.require_admin)):
    u = session.get(User, user_id)
    if not u or u.id == admin.id:
        raise HTTPException(400, "You can't ban yourself.")
    u.banned = not u.banned
    u.session_version += 1
    if u.banned:
        for ev in session.exec(select(Event).where(Event.owner_id == u.id, Event.status == "published")):
            ev.status = "draft"
            ev.visibility = "unlisted"
            session.add(ev)
    session.add(u)
    session.commit()
    return redirect(request, f"/admin?q={quote(u.email)}&msg={quote(('Banned ' if u.banned else 'Unbanned ') + u.email)}")
