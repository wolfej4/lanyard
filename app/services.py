"""Registration rules in one place, so the web routes, the Stripe webhook and the
background worker all agree on what counts as a taken spot."""
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, func, select

from . import mailer
from .config import settings
from .models import ACTIVE_STATUSES as ACTIVE, Event, Registration, TicketType, User, utcnow
from .util import blocked_set, fmt_dt, money, seat_count, ticket_code


class RegistrationError(Exception):
    pass


def _live_filter(now):
    """Confirmed, or pending with an unexpired checkout hold."""
    return or_(
        Registration.status == "confirmed",
        (Registration.status == "pending") & (Registration.hold_expires_at > now),
    )


def taken_count(session: Session, event_id: int, ticket_type_id: Optional[int] = None) -> int:
    q = select(func.count()).select_from(Registration).where(
        Registration.event_id == event_id, _live_filter(utcnow()))
    if ticket_type_id is not None:
        q = q.where(Registration.ticket_type_id == ticket_type_id)
    return session.exec(q).one()


def confirmed_count(session: Session, event_id: int) -> int:
    return session.exec(select(func.count()).select_from(Registration).where(
        Registration.event_id == event_id, Registration.status == "confirmed")).one()


def effective_capacity(event: Event) -> int:
    if event.seating_enabled:
        return min(event.capacity, seat_count(event.seat_rows, event.seat_cols, blocked_set(event.seat_blocked)))
    return event.capacity


def needs_approval(event: Event) -> bool:
    limit = settings.free_capacity_limit
    return bool(limit) and event.capacity > limit and not event.large_event_approved


@dataclass
class RegState:
    code: str      # open | draft | cancelled | ended | not_open | closed | sold_out
    message: str


def registration_state(session: Session, event: Event) -> RegState:
    now = utcnow()
    if event.status == "cancelled":
        return RegState("cancelled", "This event was cancelled.")
    if event.status != "published":
        return RegState("draft", "This event isn't published yet.")
    if event.end_at < now:
        return RegState("ended", "This event has ended.")
    if event.registration_opens_at and event.registration_opens_at > now:
        return RegState("not_open", f"Registration opens {fmt_dt(event.registration_opens_at, event.timezone)}.")
    closes = event.registration_closes_at or event.start_at
    if closes < now:
        return RegState("closed", "Registration is closed.")
    if taken_count(session, event.id) >= effective_capacity(event):
        return RegState("sold_out", "Every spot is taken.")
    return RegState("open", "")


def ticket_available(session: Session, event: Event, ticket: TicketType) -> bool:
    if not ticket.active:
        return False
    if ticket.quantity is not None and taken_count(session, event.id, ticket.id) >= ticket.quantity:
        return False
    return True


def live_registration(session: Session, event_id: int, user_id: int) -> Optional[Registration]:
    return session.exec(select(Registration).where(
        Registration.event_id == event_id, Registration.user_id == user_id,
        Registration.status.in_(("pending", "confirmed")))).first()


def create_registration(session: Session, event_id: int, user: User, ticket_id: int,
                        gamer_tag: str) -> Registration:
    # Lock the event row so two people can't both take the last spot (Postgres).
    event = session.exec(select(Event).where(Event.id == event_id).with_for_update()).one()
    ticket = session.get(TicketType, ticket_id)
    if not ticket or ticket.event_id != event.id:
        raise RegistrationError("Pick a ticket type.")

    existing = live_registration(session, event.id, user.id)
    if existing:
        if existing.status == "confirmed":
            raise RegistrationError("You're already registered for this event.")
        if existing.hold_expires_at and existing.hold_expires_at > utcnow():
            return existing  # resume the unpaid checkout
        existing.status = "cancelled"
        existing.cancelled_at = utcnow()
        session.add(existing)
        session.flush()

    state = registration_state(session, event)
    if state.code != "open":
        raise RegistrationError(state.message)
    if not ticket_available(session, event, ticket):
        raise RegistrationError(f"{ticket.name} tickets are sold out.")
    if event.owner_id == user.id and ticket.price_cents:
        raise RegistrationError("Organizers can't buy tickets to their own event. Add yourself with a free ticket type.")

    paid = ticket.price_cents > 0
    if paid:
        if not settings.payments_enabled:
            raise RegistrationError("Paid tickets aren't available on this site yet.")
        organizer = session.get(User, event.owner_id)
        if not settings.dev_fake_payments and not (organizer and organizer.stripe_charges_enabled):
            raise RegistrationError("The organizer hasn't finished payment setup, so paid tickets can't be sold yet.")

    reg = Registration(
        event_id=event.id, user_id=user.id, ticket_type_id=ticket.id, code=ticket_code(),
        gamer_tag=gamer_tag.strip()[:40], amount_cents=ticket.price_cents,
        status="pending" if paid else "confirmed",
        hold_expires_at=utcnow() + timedelta(minutes=settings.checkout_hold_minutes) if paid else None,
        confirmed_at=None if paid else utcnow(),
    )
    session.add(reg)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise RegistrationError("You're already registered for this event.")
    if not paid:
        queue_confirmation(session, reg, event, ticket)
    session.commit()
    session.refresh(reg)
    return reg


def queue_confirmation(session: Session, reg: Registration, event: Event, ticket: TicketType) -> None:
    user = session.get(User, reg.user_id)
    mailer.queue(session, user.email, "confirmed.txt", kind="confirmed", event_id=event.id,
                 event=event, reg=reg, ticket_name=ticket.name, price=money(reg.amount_cents),
                 when=fmt_dt(event.start_at, event.timezone))


def confirm_paid(session: Session, reg: Registration, payment_intent: Optional[str]) -> None:
    """Called from the webhook. Idempotent. If the hold had lapsed and the spot was
    resold, the payment is still honored: we'd rather briefly overfill than take
    money for nothing. The organizer sees the overage on the dashboard."""
    if reg.status == "confirmed":
        return
    reg.status = "confirmed"
    reg.confirmed_at = utcnow()
    reg.hold_expires_at = None
    if payment_intent:
        reg.stripe_payment_intent = payment_intent
    session.add(reg)
    event = session.get(Event, reg.event_id)
    queue_confirmation(session, reg, event, session.get(TicketType, reg.ticket_type_id))
    session.commit()


def cancel(session: Session, reg: Registration, refund: bool, notify: bool = True) -> bool:
    """Cancel a registration, refunding through Stripe if asked. Returns True if a refund was issued."""
    from . import payments

    refunded = False
    if refund and reg.status == "confirmed" and reg.amount_cents and reg.stripe_payment_intent:
        payments.refund(reg)
        refunded = True
    elif refund and reg.status == "confirmed" and reg.amount_cents and settings.dev_fake_payments:
        refunded = True
    reg.status = "refunded" if refunded else "cancelled"
    reg.cancelled_at = utcnow()
    reg.seat = None
    session.add(reg)
    if notify:
        event = session.get(Event, reg.event_id)
        user = session.get(User, reg.user_id)
        mailer.queue(session, user.email, "registration_cancelled.txt", kind="cancelled",
                     event_id=event.id, event=event, reg=reg, refunded=refunded,
                     price=money(reg.amount_cents))
    session.commit()
    return refunded


def expire_holds(session: Session) -> int:
    stale = session.exec(select(Registration).where(
        Registration.status == "pending", Registration.hold_expires_at <= utcnow())).all()
    for reg in stale:
        reg.status = "cancelled"
        reg.cancelled_at = utcnow()
        reg.seat = None
        session.add(reg)
    session.commit()
    return len(stale)


# ---- Account deletion ---------------------------------------------------------

def deletion_blockers(session: Session, user: User) -> list[str]:
    """Things that have to be resolved first, so nobody is left holding a ticket
    to an event whose organizer vanished, or a paid ticket with no owner."""
    now = utcnow()
    problems = []
    regs = session.exec(select(Registration, Event).join(Event, Event.id == Registration.event_id).where(
        Registration.user_id == user.id, Registration.status.in_(ACTIVE),
        Event.end_at > now, Event.status != "cancelled")).all()
    for reg, ev in regs:
        problems.append(f"You're registered for {ev.title}. Cancel that registration first"
                        + (" (paid tickets: request a refund from the organizer)." if reg.amount_cents else "."))
    hosting = session.exec(select(Event).where(Event.owner_id == user.id, Event.status == "published",
                                               Event.end_at > now)).all()
    for ev in hosting:
        problems.append(f"You're hosting {ev.title}. Cancel the event first; attendees are notified and refunded.")
    return problems


def delete_account(session: Session, user: User) -> None:
    """Removes personal data and keeps the anonymous bookkeeping that payments,
    capacity counts and past events depend on."""
    from sqlmodel import delete
    from .models import EmailOutbox, LoginToken, PollVote, TournamentEntry
    old_email = user.email
    session.exec(delete(PollVote).where(PollVote.user_id == user.id))
    session.exec(delete(TournamentEntry).where(TournamentEntry.user_id == user.id))
    session.exec(delete(LoginToken).where(LoginToken.email == old_email))
    session.exec(delete(EmailOutbox).where(EmailOutbox.to_addr == old_email))
    for reg in session.exec(select(Registration).where(Registration.user_id == user.id)):
        reg.gamer_tag = ""
        reg.seat = None
        session.add(reg)
    for ev in session.exec(select(Event).where(Event.owner_id == user.id, Event.status == "draft")):
        ev.status = "cancelled"
        ev.visibility = "unlisted"
        session.add(ev)
    import secrets as _secrets
    user.email = f"deleted-{user.id}-{_secrets.token_hex(4)}@deleted.invalid"
    user.display_name = ""
    user.stripe_account_id = None
    user.stripe_charges_enabled = False
    user.is_admin = False
    user.banned = True
    user.deleted_at = utcnow()
    user.session_version += 1
    session.add(user)
    session.commit()
