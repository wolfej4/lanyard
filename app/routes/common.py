from typing import Optional

from fastapi import HTTPException
from sqlmodel import Session, func, select

from .. import services
from ..models import (Announcement, Event, Poll, PollOption, PollVote, Registration, TicketType,
                      Tournament, TournamentEntry, User)
from ..util import blocked_set, seat_grid


def event_by_slug(session: Session, slug: str, viewer: Optional[User]) -> Event:
    ev = session.exec(select(Event).where(Event.slug == slug)).first()
    if not ev:
        raise HTTPException(404)
    if ev.status == "draft" and not (viewer and (viewer.id == ev.owner_id or viewer.is_admin)):
        raise HTTPException(404)
    return ev


def owned_event(session: Session, event_id: int, user: User) -> Event:
    ev = session.get(Event, event_id)
    if not ev or (ev.owner_id != user.id and not user.is_admin):
        raise HTTPException(404)
    return ev


def seat_map(session: Session, ev: Event, mine: Optional[str] = None) -> dict:
    taken = {
        r.seat: r for r in session.exec(select(Registration).where(
            Registration.event_id == ev.id, Registration.seat.is_not(None),
            Registration.status.in_(services.ACTIVE)))
    }
    names = {}
    if ev.show_attendees and taken:
        users = {u.id: u for u in session.exec(select(User).where(User.id.in_([r.user_id for r in taken.values()])))}
        names = {seat: (r.gamer_tag or users[r.user_id].name) for seat, r in taken.items()}
    return {
        "grid": seat_grid(ev.seat_rows, ev.seat_cols, blocked_set(ev.seat_blocked)),
        "taken": set(taken), "names": names, "mine": mine,
    }


def poll_view(session: Session, poll: Poll, user: Optional[User]) -> dict:
    options = session.exec(select(PollOption).where(PollOption.poll_id == poll.id).order_by(PollOption.id)).all()
    counts = dict(session.exec(select(PollVote.option_id, func.count()).where(PollVote.poll_id == poll.id)
                               .group_by(PollVote.option_id)).all())
    total = sum(counts.values())
    mine = None
    if user:
        v = session.exec(select(PollVote).where(PollVote.poll_id == poll.id, PollVote.user_id == user.id)).first()
        mine = v.option_id if v else None
    return {"poll": poll, "total": total, "mine": mine, "options": [
        {"id": o.id, "label": o.label, "count": counts.get(o.id, 0),
         "pct": round(100 * counts.get(o.id, 0) / total) if total else 0} for o in options]}


def tournament_view(session: Session, t: Tournament, user: Optional[User]) -> dict:
    entries = session.exec(select(TournamentEntry).where(TournamentEntry.tournament_id == t.id)
                           .order_by(TournamentEntry.created_at)).all()
    users = {u.id: u for u in session.exec(select(User).where(User.id.in_([e.user_id for e in entries])))} if entries else {}
    tags = {r.user_id: r.gamer_tag for r in session.exec(select(Registration).where(
        Registration.event_id == t.event_id, Registration.status == "confirmed"))}
    people = [{"name": tags.get(e.user_id) or users[e.user_id].name, "team": e.team_no, "me": user and e.user_id == user.id}
              for e in entries]
    teams = {}
    for p in people:
        if p["team"]:
            teams.setdefault(p["team"], []).append(p["name"])
    joined = any(p["me"] for p in people)
    full = t.max_entrants is not None and len(people) >= t.max_entrants
    return {"t": t, "people": people, "teams": dict(sorted(teams.items())), "joined": joined, "full": full}


def event_page_context(session: Session, ev: Event, user: Optional[User]) -> dict:
    tickets = session.exec(select(TicketType).where(TicketType.event_id == ev.id, TicketType.active)
                           .order_by(TicketType.sort, TicketType.id)).all()
    my_reg = services.live_registration(session, ev.id, user.id) if user else None
    attending = bool(my_reg and my_reg.status == "confirmed")
    attendees = []
    if ev.show_attendees:
        rows = session.exec(select(Registration, User).join(User, User.id == Registration.user_id)
                            .where(Registration.event_id == ev.id, Registration.status == "confirmed")
                            .order_by(Registration.confirmed_at)).all()
        attendees = [r.gamer_tag or u.name for r, u in rows]
    capacity = services.effective_capacity(ev)
    taken = services.taken_count(session, ev.id)
    return {
        "ev": ev, "tickets": [{"t": t, "available": services.ticket_available(session, ev, t)} for t in tickets],
        "state": services.registration_state(session, ev), "my_reg": my_reg, "attending": attending,
        "is_owner": bool(user and user.id == ev.owner_id),
        "capacity": capacity, "taken": taken, "left": max(0, capacity - taken),
        "attendees": attendees,
        "announcements": session.exec(select(Announcement).where(Announcement.event_id == ev.id)
                                      .order_by(Announcement.created_at.desc())).all(),
        "polls": [poll_view(session, p, user) for p in session.exec(
            select(Poll).where(Poll.event_id == ev.id).order_by(Poll.created_at.desc())).all()],
        "tournaments": [tournament_view(session, t, user) for t in session.exec(
            select(Tournament).where(Tournament.event_id == ev.id).order_by(Tournament.starts_at, Tournament.id)).all()],
        "seats": seat_map(session, ev, my_reg.seat if my_reg else None) if ev.seating_enabled else None,
        "organizer": session.get(User, ev.owner_id),
    }
