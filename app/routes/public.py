from typing import Optional

from fastapi import APIRouter, Depends, Request
from sqlalchemy import or_
from sqlmodel import Session, func, select

from .. import services
from ..auth import get_current_user
from ..db import get_session
from ..models import Event, Registration, User, utcnow
from ..web import render
from .common import event_by_slug, event_page_context

router = APIRouter()
PAGE = 20


def _search(session: Session, q: str, region: str, page: int):
    now = utcnow()
    stmt = select(Event).where(Event.status == "published", Event.visibility == "public", Event.end_at > now)
    if q:
        like = f"%{q.strip()[:60]}%"
        stmt = stmt.where(or_(Event.title.ilike(like), Event.tagline.ilike(like), Event.city.ilike(like),
                              Event.venue_name.ilike(like), Event.region.ilike(like)))
    if region:
        stmt = stmt.where(func.lower(Event.region) == region.lower())
    events = session.exec(stmt.order_by(Event.start_at).offset(page * PAGE).limit(PAGE + 1)).all()
    more = len(events) > PAGE
    events = events[:PAGE]
    counts = {}
    if events:
        counts = dict(session.exec(
            select(Registration.event_id, func.count()).where(
                Registration.event_id.in_([e.id for e in events]), Registration.status == "confirmed")
            .group_by(Registration.event_id)).all())
    rows = [{"ev": e, "going": counts.get(e.id, 0), "capacity": services.effective_capacity(e)} for e in events]
    return rows, more


def _regions(session: Session) -> list[str]:
    now = utcnow()
    return [r for r in session.exec(
        select(Event.region).where(Event.status == "published", Event.visibility == "public",
                                   Event.end_at > now, Event.region != "").distinct().order_by(Event.region)).all()]


@router.get("/")
def home(request: Request, session: Session = Depends(get_session),
         user: Optional[User] = Depends(get_current_user)):
    rows, more = _search(session, "", "", 0)
    return render(request, "home.html", rows=rows, more=more, regions=_regions(session), q="", region="", page=0)


@router.get("/events")
def events(request: Request, q: str = "", region: str = "", page: int = 0,
           session: Session = Depends(get_session), user: Optional[User] = Depends(get_current_user)):
    page = max(0, page)
    rows, more = _search(session, q, region, page)
    ctx = dict(rows=rows, more=more, q=q, region=region, page=page, regions=_regions(session))
    if request.headers.get("hx-request") == "true" and request.headers.get("hx-target") == "results":
        return render(request, "partials/event_rows.html", **ctx)
    return render(request, "events.html", **ctx)


@router.get("/terms")
def terms(request: Request, user: Optional[User] = Depends(get_current_user)):
    return render(request, "legal/terms.html")


@router.get("/privacy")
def privacy(request: Request, user: Optional[User] = Depends(get_current_user)):
    return render(request, "legal/privacy.html")


@router.get("/e/{slug}")
def event_page(slug: str, request: Request, session: Session = Depends(get_session),
               user: Optional[User] = Depends(get_current_user)):
    ev = event_by_slug(session, slug, user)
    return render(request, "event.html", **event_page_context(session, ev, user),
                  checkout_cancelled=request.query_params.get("checkout") == "cancelled",
                  error=request.query_params.get("error"))
