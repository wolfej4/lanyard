import csv
import io
import json
import random
import re
from urllib.parse import quote

import stripe
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlmodel import Session, func, select

from .. import auth, mailer, payments, services
from ..config import settings
from ..db import get_session
from ..models import (Announcement, Event, Poll, PollOption, PollVote, Registration, TicketType,
                      Tournament, TournamentEntry, User, utcnow)
from ..util import (COMMON_TIMEZONES, blocked_set, fmt_dt, local_to_utc, money, parse_money,
                    seat_grid, slugify, ticket_code, valid_tz)
from ..web import redirect, render
from .account import EMAIL_RE
from .common import owned_event, poll_view, seat_map, tournament_view

router = APIRouter(prefix="/manage", dependencies=[Depends(auth.csrf_protect)])
TABS = ["overview", "details", "tickets", "seating", "attendees", "announcements", "polls", "tournaments"]


def _back(ev: Event, tab: str, msg: str = "", error: str = "") -> str:
    url = f"/manage/{ev.id}?tab={tab}"
    if msg:
        url += f"&msg={quote(msg)}"
    if error:
        url += f"&error={quote(error)}"
    return url


# ---- Event list & creation --------------------------------------------------

@router.get("")
def index(request: Request, session: Session = Depends(get_session), user: User = Depends(auth.require_user)):
    events = session.exec(select(Event).where(Event.owner_id == user.id).order_by(Event.start_at.desc())).all()
    counts = dict(session.exec(select(Registration.event_id, func.count()).where(
        Registration.event_id.in_([e.id for e in events] or [0]), Registration.status == "confirmed")
        .group_by(Registration.event_id)).all())
    now = utcnow()
    return render(request, "manage/index.html", now=now,
                  upcoming=[(e, counts.get(e.id, 0)) for e in events if e.end_at >= now][::-1],
                  past=[(e, counts.get(e.id, 0)) for e in events if e.end_at < now])


def _event_form_values(form: dict, ev: Event | None = None) -> tuple[dict, list[str]]:
    errors = []
    tz = form.get("timezone", "").strip()
    if not valid_tz(tz):
        errors.append("Pick a time zone.")
        tz = "America/Chicago"
    title = form.get("title", "").strip()
    if not 3 <= len(title) <= 120:
        errors.append("Give the event a name between 3 and 120 characters.")
    start = local_to_utc(form.get("start_at", ""), tz)
    end = local_to_utc(form.get("end_at", ""), tz)
    if not start or not end:
        errors.append("Set a start and end time.")
    elif end <= start:
        errors.append("The end time has to be after the start time.")
    elif (end - start).days > 14:
        errors.append("Events can run for up to 14 days.")
    try:
        capacity = int(form.get("capacity", "0"))
    except ValueError:
        capacity = 0
    if not 2 <= capacity <= settings.max_capacity:
        errors.append(f"Capacity has to be between 2 and {settings.max_capacity}.")
    opens = local_to_utc(form.get("registration_opens_at", ""), tz)
    closes = local_to_utc(form.get("registration_closes_at", ""), tz)
    if opens and closes and closes <= opens:
        errors.append("Registration has to close after it opens.")
    vals = dict(
        title=title, tagline=form.get("tagline", "").strip()[:160],
        description=form.get("description", "").strip()[:10000],
        bring_notes=form.get("bring_notes", "").strip()[:3000],
        venue_name=form.get("venue_name", "").strip()[:120], address=form.get("address", "").strip()[:200],
        city=form.get("city", "").strip()[:80], region=form.get("region", "").strip()[:80],
        country=(form.get("country", "US").strip().upper() or "US")[:2], timezone=tz,
        start_at=start, end_at=end, capacity=capacity,
        visibility="unlisted" if form.get("visibility") == "unlisted" else "public",
        registration_opens_at=opens, registration_closes_at=closes,
        show_attendees=form.get("show_attendees") == "on",
    )
    return vals, errors


@router.get("/new")
def new_form(request: Request, user: User = Depends(auth.require_user)):
    return render(request, "manage/event_form.html", ev=None, f=None, errors=[], timezones=COMMON_TIMEZONES)


@router.post("/new")
async def create(request: Request, session: Session = Depends(get_session), user: User = Depends(auth.require_user)):
    form = dict(await request.form())
    vals, errors = _event_form_values(form)
    if errors:
        return render(request, "manage/event_form.html", status_code=422, ev=None, f=form,
                      errors=errors, timezones=COMMON_TIMEZONES)
    ev = Event(owner_id=user.id, slug=slugify(vals["title"]), **vals)
    session.add(ev)
    session.flush()
    session.add(TicketType(event_id=ev.id, name="General admission", price_cents=0))
    session.commit()
    return RedirectResponse(_back(ev, "overview", "Draft created. Add tickets, then publish when you're ready."), 303)


# ---- Dashboard ----------------------------------------------------------------

@router.get("/{event_id}")
def dashboard(event_id: int, request: Request, tab: str = "overview", session: Session = Depends(get_session),
              user: User = Depends(auth.require_user)):
    ev = owned_event(session, event_id, user)
    tab = tab if tab in TABS else "overview"
    ctx = dict(ev=ev, tab=tab, tabs=TABS, msg=request.query_params.get("msg"),
               error=request.query_params.get("error"), owner=session.get(User, ev.owner_id))
    tickets = session.exec(select(TicketType).where(TicketType.event_id == ev.id)
                           .order_by(TicketType.sort, TicketType.id)).all()
    sold = dict(session.exec(select(Registration.ticket_type_id, func.count()).where(
        Registration.event_id == ev.id, Registration.status == "confirmed").group_by(Registration.ticket_type_id)).all())
    confirmed = services.confirmed_count(session, ev.id)
    revenue = session.exec(select(func.coalesce(func.sum(Registration.amount_cents), 0)).where(
        Registration.event_id == ev.id, Registration.status == "confirmed")).one()
    ctx.update(tickets=tickets, sold=sold, confirmed=confirmed, revenue=revenue,
               capacity=services.effective_capacity(ev), needs_approval=services.needs_approval(ev),
               has_paid=any(t.price_cents and t.active for t in tickets))

    if tab == "details":
        ctx.update(f=None, errors=[], timezones=COMMON_TIMEZONES)
    elif tab == "seating":
        ctx.update(grid=seat_grid(ev.seat_rows, ev.seat_cols, blocked_set(ev.seat_blocked)),
                   seats=seat_map(session, ev))
    elif tab == "attendees":
        ctx["regs"] = session.exec(
            select(Registration, User, TicketType).join(User, User.id == Registration.user_id)
            .join(TicketType, TicketType.id == Registration.ticket_type_id)
            .where(Registration.event_id == ev.id).order_by(Registration.created_at.desc())).all()
    elif tab == "announcements":
        ctx["announcements"] = session.exec(select(Announcement).where(Announcement.event_id == ev.id)
                                            .order_by(Announcement.created_at.desc())).all()
    elif tab == "polls":
        ctx["polls"] = [poll_view(session, p, None) for p in session.exec(
            select(Poll).where(Poll.event_id == ev.id).order_by(Poll.created_at.desc()))]
    elif tab == "tournaments":
        ctx["tournaments"] = [tournament_view(session, t, None) for t in session.exec(
            select(Tournament).where(Tournament.event_id == ev.id).order_by(Tournament.id))]
    return render(request, "manage/dashboard.html", **ctx)


@router.post("/{event_id}/details")
async def update_details(event_id: int, request: Request, session: Session = Depends(get_session),
                         user: User = Depends(auth.require_user)):
    ev = owned_event(session, event_id, user)
    form = dict(await request.form())
    vals, errors = _event_form_values(form, ev)
    if not errors and vals["capacity"] < services.confirmed_count(session, ev.id):
        errors.append("Capacity can't go below the number of people already registered.")
    if errors:
        return render(request, "manage/dashboard.html", status_code=422, ev=ev, tab="details", tabs=TABS,
                      f=form, errors=errors, timezones=COMMON_TIMEZONES, msg=None, error=None,
                      tickets=[], sold={}, confirmed=0, revenue=0, capacity=ev.capacity, needs_approval=False,
                      has_paid=False, owner=user)
    if vals["start_at"] != ev.start_at:
        ev.reminder_week_sent = ev.reminder_day_sent = False
    if vals["capacity"] != ev.capacity and vals["capacity"] > settings.free_capacity_limit:
        pass  # approval flag stays; admin can revoke
    for k, v in vals.items():
        setattr(ev, k, v)
    ev.updated_at = utcnow()
    session.add(ev)
    session.commit()
    return RedirectResponse(_back(ev, "details", "Saved."), 303)


@router.post("/{event_id}/status")
def set_status(event_id: int, request: Request, action: str = Form(...), session: Session = Depends(get_session),
               user: User = Depends(auth.require_user)):
    ev = owned_event(session, event_id, user)
    if action == "publish":
        if ev.status == "cancelled":
            return redirect(request, _back(ev, "overview", error="Cancelled events can't be published again."))
        if services.needs_approval(ev):
            return redirect(request, _back(ev, "overview", error=(
                f"Events over {settings.free_capacity_limit} people need a quick review before they go live. "
                "We've flagged it for the site admin.")))
        has_ticket = session.exec(select(TicketType).where(TicketType.event_id == ev.id, TicketType.active)).first()
        if not has_ticket:
            return redirect(request, _back(ev, "tickets", error="Add at least one ticket type before publishing."))
        paid = session.exec(select(TicketType).where(TicketType.event_id == ev.id, TicketType.active,
                                                     TicketType.price_cents > 0)).first()
        organizer = session.get(User, ev.owner_id)
        if paid and not (settings.dev_fake_payments or organizer.stripe_charges_enabled):
            return redirect(request, _back(ev, "tickets", error=(
                "You have a paid ticket type, so finish payment setup first (Payments, top right), "
                "or make every ticket free.")))
        ev.status = "published"
        msg = "Published. Share the link and people can register."
    elif action == "unpublish":
        if services.confirmed_count(session, ev.id):
            return redirect(request, _back(ev, "overview", error=(
                "People are already registered. Cancel the event instead if it isn't happening.")))
        ev.status = "draft"
        msg = "Back to draft. Only you can see it."
    elif action == "cancel":
        if ev.status == "cancelled":
            return redirect(request, _back(ev, "overview"))
        regs = session.exec(select(Registration).where(
            Registration.event_id == ev.id, Registration.status.in_(("pending", "confirmed")))).all()
        failed = 0
        for reg in regs:
            was_confirmed = reg.status == "confirmed"
            try:
                refunded = services.cancel(session, reg, refund=True, notify=False)
            except stripe.StripeError:
                failed += 1
                continue
            if was_confirmed:
                attendee = session.get(User, reg.user_id)
                mailer.queue(session, attendee.email, "cancelled.txt", kind="event_cancelled", event_id=ev.id,
                             event=ev, refunded=refunded, price=money(reg.amount_cents),
                             when=fmt_dt(ev.start_at, ev.timezone))
        ev.status = "cancelled"
        session.add(ev)
        session.commit()
        if failed:
            return redirect(request, _back(ev, "attendees", error=(
                f"Event cancelled, but {failed} refund(s) failed in Stripe. Retry those from the attendee list.")))
        return redirect(request, _back(ev, "overview", "Event cancelled. Attendees were emailed and paid tickets refunded."))
    else:
        raise HTTPException(400)
    session.add(ev)
    session.commit()
    return redirect(request, _back(ev, "overview", msg))


# ---- Tickets ------------------------------------------------------------------

@router.post("/{event_id}/tickets")
def add_ticket(event_id: int, request: Request, name: str = Form(""), price: str = Form("0"),
               quantity: str = Form(""), description: str = Form(""), ticket_id: int | None = Form(None),
               session: Session = Depends(get_session), user: User = Depends(auth.require_user)):
    ev = owned_event(session, event_id, user)
    cents = parse_money(price)
    name = name.strip()[:60]
    if not name:
        return redirect(request, _back(ev, "tickets", error="Name the ticket type, like General admission or BYOC seat."))
    if cents is None:
        return redirect(request, _back(ev, "tickets", error="Enter a price like 15 or 15.00, or 0 for free."))
    if 0 < cents < 100:
        return redirect(request, _back(ev, "tickets", error="Paid tickets have to cost at least $1."))
    qty = int(quantity) if quantity.strip().isdigit() and int(quantity) > 0 else None
    if ticket_id:
        t = session.get(TicketType, ticket_id)
        if not t or t.event_id != ev.id:
            raise HTTPException(404)
        sold = services.taken_count(session, ev.id, t.id)
        if sold and cents != t.price_cents:
            return redirect(request, _back(ev, "tickets", error=(
                "People already bought this ticket type, so its price is locked. Add a new type instead.")))
        if qty is not None and qty < sold:
            return redirect(request, _back(ev, "tickets", error=f"{sold} are already taken; the limit can't be lower."))
    else:
        t = TicketType(event_id=ev.id, sort=len(session.exec(select(TicketType).where(TicketType.event_id == ev.id)).all()))
    t.name, t.price_cents, t.quantity, t.description = name, cents, qty, description.strip()[:200]
    session.add(t)
    session.commit()
    return redirect(request, _back(ev, "tickets", "Ticket type saved."))


@router.post("/{event_id}/tickets/{ticket_id}/toggle")
def toggle_ticket(event_id: int, ticket_id: int, request: Request, session: Session = Depends(get_session),
                  user: User = Depends(auth.require_user)):
    ev = owned_event(session, event_id, user)
    t = session.get(TicketType, ticket_id)
    if not t or t.event_id != ev.id:
        raise HTTPException(404)
    t.active = not t.active
    session.add(t)
    session.commit()
    return redirect(request, _back(ev, "tickets", f"{t.name} is now {'on sale' if t.active else 'hidden'}."))


# ---- Seating ------------------------------------------------------------------

@router.post("/{event_id}/seating")
def seating_settings(event_id: int, request: Request, enabled: str = Form(""), rows: int = Form(4),
                     cols: int = Form(8), session: Session = Depends(get_session),
                     user: User = Depends(auth.require_user)):
    ev = owned_event(session, event_id, user)
    rows, cols = max(1, min(rows, 40)), max(1, min(cols, 40))
    seated = session.exec(select(Registration.seat).where(
        Registration.event_id == ev.id, Registration.seat.is_not(None),
        Registration.status.in_(services.ACTIVE))).all()
    if (rows < ev.seat_rows or cols < ev.seat_cols) and seated:
        valid = {c["label"] for row in seat_grid(rows, cols, []) for c in row}
        lost = [s for s in seated if s not in valid]
        if lost:
            return redirect(request, _back(ev, "seating", error=(
                f"Shrinking the grid would remove seats people already picked ({', '.join(lost[:5])}).")))
    ev.seating_enabled = enabled == "on"
    ev.seat_rows, ev.seat_cols = rows, cols
    valid = {c["label"] for row in seat_grid(rows, cols, []) for c in row}
    ev.seat_blocked = json.dumps(sorted(b for b in blocked_set(ev.seat_blocked) if b in valid))
    session.add(ev)
    session.commit()
    return redirect(request, _back(ev, "seating", "Seating saved."))


@router.post("/{event_id}/seating/toggle")
def toggle_cell(event_id: int, request: Request, seat: str = Form(...), session: Session = Depends(get_session),
                user: User = Depends(auth.require_user)):
    ev = owned_event(session, event_id, user)
    blocked = blocked_set(ev.seat_blocked)
    seat = seat.upper()
    taken = seat_map(session, ev)["taken"]
    error = None
    if seat in taken:
        error = f"{seat} is taken by an attendee, so it can't become a gap."
    elif seat in blocked:
        blocked.discard(seat)
    else:
        blocked.add(seat)
    ev.seat_blocked = json.dumps(sorted(blocked))
    session.add(ev)
    session.commit()
    return render(request, "manage/partials/seat_editor.html", ev=ev, seats=seat_map(session, ev),
                  grid=seat_grid(ev.seat_rows, ev.seat_cols, blocked), seat_error=error,
                  capacity=services.effective_capacity(ev))


# ---- Attendees ----------------------------------------------------------------

@router.get("/{event_id}/attendees.csv")
def attendees_csv(event_id: int, session: Session = Depends(get_session), user: User = Depends(auth.require_user)):
    ev = owned_event(session, event_id, user)
    rows = session.exec(
        select(Registration, User, TicketType).join(User, User.id == Registration.user_id)
        .join(TicketType, TicketType.id == Registration.ticket_type_id)
        .where(Registration.event_id == ev.id, Registration.status == "confirmed")
        .order_by(Registration.confirmed_at)).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ticket_code", "name", "gamer_tag", "email", "ticket_type", "paid", "seat", "registered_at_utc"])
    for reg, u, t in rows:
        w.writerow([reg.code, u.name, reg.gamer_tag, u.email, t.name, f"{reg.amount_cents / 100:.2f}",
                    reg.seat or "", reg.confirmed_at.isoformat(timespec="minutes") if reg.confirmed_at else ""])
    fname = re.sub(r"[^a-z0-9-]", "", ev.slug) + "-attendees.csv"
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.post("/{event_id}/attendees/add")
def add_attendee(event_id: int, request: Request, email: str = Form(""), gamer_tag: str = Form(""),
                 session: Session = Depends(get_session), user: User = Depends(auth.require_user)):
    """Comp someone in: no payment, skips the registration window, still counts toward capacity."""
    ev = owned_event(session, event_id, user)
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        return redirect(request, _back(ev, "attendees", error="Enter the person's email address."))
    if services.taken_count(session, ev.id) >= services.effective_capacity(ev):
        return redirect(request, _back(ev, "attendees", error="The event is full. Raise capacity first."))
    person = session.exec(select(User).where(User.email == email)).first()
    if not person:
        person = User(email=email)
        session.add(person)
        session.flush()
    if services.live_registration(session, ev.id, person.id):
        return redirect(request, _back(ev, "attendees", error=f"{email} is already registered."))
    comp = session.exec(select(TicketType).where(TicketType.event_id == ev.id, TicketType.name == "Comp")).first()
    if not comp:
        comp = TicketType(event_id=ev.id, name="Comp", price_cents=0, active=False, sort=99)
        session.add(comp)
        session.flush()
    reg = Registration(event_id=ev.id, user_id=person.id, ticket_type_id=comp.id, code=ticket_code(),
                       gamer_tag=gamer_tag.strip()[:40], status="confirmed", confirmed_at=utcnow())
    session.add(reg)
    session.flush()
    services.queue_confirmation(session, reg, ev, comp)
    session.commit()
    return redirect(request, _back(ev, "attendees", f"Added {email}. They've been emailed their ticket."))


@router.post("/{event_id}/attendees/{reg_id}/cancel")
def cancel_attendee(event_id: int, reg_id: int, request: Request, refund: str = Form(""),
                    session: Session = Depends(get_session), user: User = Depends(auth.require_user)):
    ev = owned_event(session, event_id, user)
    reg = session.get(Registration, reg_id)
    if not reg or reg.event_id != ev.id or reg.status not in ("pending", "confirmed"):
        raise HTTPException(404)
    try:
        refunded = services.cancel(session, reg, refund=refund == "on")
    except stripe.StripeError as e:
        return redirect(request, _back(ev, "attendees", error=f"Stripe refused the refund: {e.user_message or e}"))
    return redirect(request, _back(ev, "attendees", f"Cancelled {reg.code}{' and refunded' if refunded else ''}."))


# ---- Announcements ------------------------------------------------------------

@router.post("/{event_id}/announcements")
def announce(event_id: int, request: Request, body: str = Form(""), email: str = Form(""),
             session: Session = Depends(get_session), user: User = Depends(auth.require_user)):
    ev = owned_event(session, event_id, user)
    body = body.strip()[:4000]
    if not body:
        return redirect(request, _back(ev, "announcements", error="Write something to post."))
    send = email == "on"
    if send:
        since = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        sent_today = session.exec(select(func.count()).select_from(Announcement).where(
            Announcement.event_id == ev.id, Announcement.emailed, Announcement.created_at >= since)).one()
        if sent_today >= settings.announcement_emails_per_day:
            return redirect(request, _back(ev, "announcements", error=(
                f"You've emailed attendees {sent_today} times today. Post it without email, or wait until tomorrow.")))
    a = Announcement(event_id=ev.id, author_id=user.id, body=body, emailed=send)
    session.add(a)
    n = 0
    if send:
        for reg in session.exec(select(Registration).where(Registration.event_id == ev.id,
                                                           Registration.status == "confirmed")):
            attendee = session.get(User, reg.user_id)
            mailer.queue(session, attendee.email, "announcement.txt", kind="announcement", event_id=ev.id,
                         event=ev, body=body, author=user.name)
            n += 1
    session.commit()
    return redirect(request, _back(ev, "announcements", f"Posted{f' and emailed to {n} attendees' if send else ''}."))


@router.post("/{event_id}/announcements/{aid}/delete")
def delete_announcement(event_id: int, aid: int, request: Request, session: Session = Depends(get_session),
                        user: User = Depends(auth.require_user)):
    ev = owned_event(session, event_id, user)
    a = session.get(Announcement, aid)
    if a and a.event_id == ev.id:
        session.delete(a)
        session.commit()
    return redirect(request, _back(ev, "announcements", "Removed from the event page."))


# ---- Polls --------------------------------------------------------------------

@router.post("/{event_id}/polls")
def create_poll(event_id: int, request: Request, question: str = Form(""), options: str = Form(""),
                session: Session = Depends(get_session), user: User = Depends(auth.require_user)):
    ev = owned_event(session, event_id, user)
    labels = [o.strip()[:100] for o in options.splitlines() if o.strip()]
    labels = list(dict.fromkeys(labels))[:12]
    if not question.strip() or len(labels) < 2:
        return redirect(request, _back(ev, "polls", error="Ask a question and give at least two options, one per line."))
    poll = Poll(event_id=ev.id, question=question.strip()[:200])
    session.add(poll)
    session.flush()
    for label in labels:
        session.add(PollOption(poll_id=poll.id, label=label))
    session.commit()
    return redirect(request, _back(ev, "polls", "Poll is live on the event page."))


@router.post("/{event_id}/polls/{pid}/{action}")
def poll_action(event_id: int, pid: int, action: str, request: Request, session: Session = Depends(get_session),
                user: User = Depends(auth.require_user)):
    ev = owned_event(session, event_id, user)
    poll = session.get(Poll, pid)
    if not poll or poll.event_id != ev.id:
        raise HTTPException(404)
    if action == "toggle":
        poll.closed = not poll.closed
        session.add(poll)
        msg = "Poll closed." if poll.closed else "Poll reopened."
    elif action == "delete":
        for v in session.exec(select(PollVote).where(PollVote.poll_id == poll.id)):
            session.delete(v)
        for o in session.exec(select(PollOption).where(PollOption.poll_id == poll.id)):
            session.delete(o)
        session.flush()
        session.delete(poll)
        msg = "Poll deleted."
    else:
        raise HTTPException(404)
    session.commit()
    return redirect(request, _back(ev, "polls", msg))


# ---- Tournaments ----------------------------------------------------------------

@router.post("/{event_id}/tournaments")
def create_tournament(event_id: int, request: Request, game: str = Form(""), format: str = Form(""),
                      team_size: int = Form(1), max_entrants: str = Form(""), starts_at: str = Form(""),
                      bracket_url: str = Form(""), session: Session = Depends(get_session),
                      user: User = Depends(auth.require_user)):
    ev = owned_event(session, event_id, user)
    if not game.strip():
        return redirect(request, _back(ev, "tournaments", error="Name the game."))
    url = bracket_url.strip()
    if url and not url.startswith("https://"):
        return redirect(request, _back(ev, "tournaments", error="Bracket links have to start with https://"))
    session.add(Tournament(
        event_id=ev.id, game=game.strip()[:80], format=format.strip()[:80], team_size=max(1, min(team_size, 16)),
        max_entrants=int(max_entrants) if max_entrants.strip().isdigit() and int(max_entrants) > 1 else None,
        starts_at=local_to_utc(starts_at, ev.timezone), bracket_url=url[:300]))
    session.commit()
    return redirect(request, _back(ev, "tournaments", "Tournament added. Attendees can sign up from the event page."))


@router.post("/{event_id}/tournaments/{tid}/{action}")
def tournament_action(event_id: int, tid: int, action: str, request: Request, bracket_url: str = Form(""),
                      session: Session = Depends(get_session), user: User = Depends(auth.require_user)):
    ev = owned_event(session, event_id, user)
    t = session.get(Tournament, tid)
    if not t or t.event_id != ev.id:
        raise HTTPException(404)
    entries = session.exec(select(TournamentEntry).where(TournamentEntry.tournament_id == t.id)).all()
    if action == "toggle":
        t.signups_open = not t.signups_open
        msg = "Signups reopened." if t.signups_open else "Signups locked."
    elif action == "teams":
        if t.team_size < 2:
            return redirect(request, _back(ev, "tournaments", error="This is a solo tournament, so there are no teams to make."))
        random.shuffle(entries)
        n_teams = max(1, -(-len(entries) // t.team_size))
        # Deal players round-robin so team sizes differ by at most one.
        for i, e in enumerate(entries):
            e.team_no = i % n_teams + 1
            session.add(e)
        t.signups_open = False
        msg = f"Shuffled {len(entries)} players into {n_teams} teams and locked signups."
    elif action == "bracket":
        url = bracket_url.strip()
        if url and not url.startswith("https://"):
            return redirect(request, _back(ev, "tournaments", error="Bracket links have to start with https://"))
        t.bracket_url = url[:300]
        msg = "Bracket link saved."
    elif action == "delete":
        for e in entries:
            session.delete(e)
        session.flush()
        session.delete(t)
        session.commit()
        return redirect(request, _back(ev, "tournaments", "Tournament deleted."))
    else:
        raise HTTPException(404)
    session.add(t)
    session.commit()
    return redirect(request, _back(ev, "tournaments", msg))


# ---- Payments (Stripe Connect onboarding) ----------------------------------------

payments_router = APIRouter(prefix="/manage/payments", dependencies=[Depends(auth.csrf_protect)])


@payments_router.get("")
def payments_page(request: Request, session: Session = Depends(get_session), user: User = Depends(auth.require_user)):
    if payments.live() and user.stripe_account_id and not user.stripe_charges_enabled:
        try:
            payments.refresh_account(session, user)
        except stripe.StripeError:
            pass
    return render(request, "manage/payments.html", error=request.query_params.get("error"))


@payments_router.post("/connect")
def connect(request: Request, session: Session = Depends(get_session), user: User = Depends(auth.require_user)):
    if not payments.live():
        return redirect(request, "/manage/payments?error=" + quote("Stripe isn't configured on this server."))
    try:
        return redirect(request, payments.onboarding_url(session, user))
    except stripe.StripeError as e:
        return redirect(request, "/manage/payments?error=" + quote(e.user_message or "Stripe didn't respond. Try again."))


@payments_router.get("/connect")
def connect_refresh(request: Request, session: Session = Depends(get_session), user: User = Depends(auth.require_user)):
    """Stripe sends people here when an onboarding link expires; mint a fresh one."""
    if not payments.live():
        raise HTTPException(404)
    try:
        return RedirectResponse(payments.onboarding_url(session, user), 303)
    except stripe.StripeError:
        return RedirectResponse("/manage/payments?error=" + quote("Stripe didn't respond. Try again."), 303)


@payments_router.get("/return")
def connect_return(session: Session = Depends(get_session), user: User = Depends(auth.require_user)):
    if payments.live():
        try:
            payments.refresh_account(session, user)
        except stripe.StripeError:
            pass
    return RedirectResponse("/manage/payments", 303)


@payments_router.post("/dashboard")
def stripe_dashboard(request: Request, user: User = Depends(auth.require_user)):
    url = payments.dashboard_url(user) if payments.live() else None
    if not url:
        return redirect(request, "/manage/payments?error=" + quote("Couldn't open the Stripe dashboard. Finish setup first."))
    return redirect(request, url)
