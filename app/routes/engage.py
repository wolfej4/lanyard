"""Things attendees do on the event page: vote in polls, join tournaments."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from .. import auth, services
from ..db import get_session
from ..models import Event, Poll, PollOption, PollVote, Tournament, TournamentEntry, User
from ..web import render
from .common import poll_view, tournament_view

router = APIRouter(dependencies=[Depends(auth.csrf_protect)])


def _must_attend(session: Session, event_id: int, user: User) -> Event:
    ev = session.get(Event, event_id)
    reg = services.live_registration(session, event_id, user.id)
    if not ev or not ((reg and reg.status == "confirmed") or ev.owner_id == user.id):
        raise HTTPException(403, "Only registered attendees can do that.")
    return ev


@router.post("/polls/{poll_id}/vote")
def vote(poll_id: int, request: Request, option_id: int = Form(...),
         session: Session = Depends(get_session), user: User = Depends(auth.require_user)):
    poll = session.get(Poll, poll_id)
    if not poll:
        raise HTTPException(404)
    ev = _must_attend(session, poll.event_id, user)
    opt = session.get(PollOption, option_id)
    if poll.closed or not opt or opt.poll_id != poll.id:
        raise HTTPException(400, "This poll is closed.")
    existing = session.exec(select(PollVote).where(PollVote.poll_id == poll.id, PollVote.user_id == user.id)).first()
    if existing:
        existing.option_id = opt.id
        session.add(existing)
    else:
        session.add(PollVote(poll_id=poll.id, option_id=opt.id, user_id=user.id))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
    return render(request, "partials/poll.html", p=poll_view(session, poll, user), attending=True, ev=ev)


@router.post("/tournaments/{tid}/{action}")
def join_leave(tid: int, action: str, request: Request, session: Session = Depends(get_session),
               user: User = Depends(auth.require_user)):
    t = session.get(Tournament, tid)
    if not t or action not in ("join", "leave"):
        raise HTTPException(404)
    ev = _must_attend(session, t.event_id, user)
    entry = session.exec(select(TournamentEntry).where(
        TournamentEntry.tournament_id == t.id, TournamentEntry.user_id == user.id)).first()
    if action == "join" and not entry:
        view = tournament_view(session, t, user)
        if not t.signups_open or view["full"]:
            raise HTTPException(400, "Signups for this tournament are closed.")
        session.add(TournamentEntry(tournament_id=t.id, user_id=user.id))
    elif action == "leave" and entry:
        if not t.signups_open:
            raise HTTPException(400, "Signups are locked. Ask the organizer to remove you.")
        session.delete(entry)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
    return render(request, "partials/tournament.html", tv=tournament_view(session, t, user), attending=True, ev=ev)
