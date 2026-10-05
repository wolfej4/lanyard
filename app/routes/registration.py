import logging
from urllib.parse import quote

import stripe
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from .. import auth, mailer, payments, services
from ..config import settings
from ..db import get_session
from ..models import Event, Registration, TicketType, User, utcnow
from ..util import blocked_set, fmt_dt, money
from ..web import redirect, render
from .common import event_by_slug, seat_map

log = logging.getLogger("lanyard.registration")
router = APIRouter(dependencies=[Depends(auth.csrf_protect)])


def _ticket_for(session: Session, code: str, user: User):
    reg = session.exec(select(Registration).where(Registration.code == code.upper())).first()
    if not reg:
        raise HTTPException(404)
    ev = session.get(Event, reg.event_id)
    if reg.user_id != user.id and ev.owner_id != user.id and not user.is_admin:
        raise HTTPException(404)
    return reg, ev


@router.post("/e/{slug}/register")
def register(slug: str, request: Request, ticket_type_id: int = Form(...), gamer_tag: str = Form(""),
             session: Session = Depends(get_session), user: User = Depends(auth.require_user)):
    ev = event_by_slug(session, slug, user)
    try:
        reg = services.create_registration(session, ev.id, user, ticket_type_id, gamer_tag)
    except services.RegistrationError as e:
        return redirect(request, f"/e/{slug}?error={quote(str(e))}")
    if reg.status == "pending":
        try:
            return redirect(request, payments.checkout_url(session, reg))
        except stripe.StripeError as e:
            log.exception("Checkout creation failed")
            return redirect(request, f"/e/{slug}?error=" + quote("Payment setup failed: " + (e.user_message or "try again shortly.")))
    return redirect(request, f"/t/{reg.code}?new=1")


@router.get("/t/{code}")
def ticket(code: str, request: Request, session: Session = Depends(get_session),
           user: User = Depends(auth.require_user)):
    reg, ev = _ticket_for(session, code, user)
    t = session.get(TicketType, reg.ticket_type_id)
    holder = session.get(User, reg.user_id)
    seats = seat_map(session, ev, reg.seat) if ev.seating_enabled else None
    return render(request, "ticket.html", reg=reg, ev=ev, ticket=t, holder=holder, seats=seats,
                  can_pick=reg.status == "confirmed" and reg.user_id == user.id and ev.end_at > utcnow()
                  and ev.status == "published",
                  just_paid=request.query_params.get("paid") == "1",
                  event_live=ev.end_at > utcnow() and ev.status == "published",
                  is_new=request.query_params.get("new") == "1",
                  msg=request.query_params.get("msg"))


@router.post("/t/{code}/pay")
def resume_payment(code: str, request: Request, session: Session = Depends(get_session),
                   user: User = Depends(auth.require_user)):
    reg, ev = _ticket_for(session, code, user)
    if reg.status != "pending" or not reg.hold_expires_at or reg.hold_expires_at < utcnow():
        return redirect(request, f"/e/{ev.slug}?error=That+checkout+expired.+Register+again+to+get+a+new+one.")
    return redirect(request, payments.checkout_url(session, reg))


@router.post("/t/{code}/seat")
def pick_seat(code: str, request: Request, seat: str = Form(...), session: Session = Depends(get_session),
              user: User = Depends(auth.require_user)):
    reg, ev = _ticket_for(session, code, user)
    if reg.user_id != user.id or reg.status != "confirmed" or not ev.seating_enabled:
        raise HTTPException(403, "You can only pick a seat on your own confirmed ticket.")
    seat = seat.strip().upper()
    valid = {c["label"] for row in seat_map(session, ev)["grid"] for c in row if not c["blocked"]}
    error = None
    if seat not in valid:
        error = "That seat doesn't exist."
    else:
        reg.seat = seat
        session.add(reg)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            error = f"Someone just took {seat}. Pick another."
        session.refresh(reg)
    return render(request, "partials/seatmap.html", seats=seat_map(session, ev, reg.seat), reg=reg,
                  ev=ev, can_pick=True, seat_error=error)


@router.post("/t/{code}/cancel")
def cancel_own(code: str, request: Request, session: Session = Depends(get_session),
               user: User = Depends(auth.require_user)):
    reg, ev = _ticket_for(session, code, user)
    if reg.user_id != user.id or reg.status not in ("pending", "confirmed"):
        raise HTTPException(403)
    if reg.status == "confirmed" and reg.amount_cents:
        raise HTTPException(403, "Paid tickets are cancelled by the organizer. Use Request a refund.")
    services.cancel(session, reg, refund=False, notify=reg.status == "confirmed")
    return redirect(request, f"/e/{ev.slug}")


@router.post("/t/{code}/refund-request")
def refund_request(code: str, request: Request, note: str = Form(""), session: Session = Depends(get_session),
                   user: User = Depends(auth.require_user)):
    reg, ev = _ticket_for(session, code, user)
    if reg.user_id != user.id or reg.status != "confirmed" or not reg.amount_cents:
        raise HTTPException(403)
    organizer = session.get(User, ev.owner_id)
    from ..models import EmailOutbox
    body = (f"{user.name} ({user.email}) asked for a refund on ticket {reg.code} for {ev.title} "
            f"({money(reg.amount_cents)}).\n\nTheir note:\n{note.strip()[:1000] or '(none)'}\n\n"
            f"To refund, cancel the registration from your attendee list:\n"
            f"{settings.base_url}/manage/{ev.id}?tab=attendees\n\n"
            f"Reply to this email to reach {user.name} directly at {user.email}.")
    session.add(EmailOutbox(to_addr=organizer.email, subject=f"Refund request for {ev.title}"[:200],
                            body=body, kind="refund_request", event_id=ev.id))
    session.commit()
    return redirect(request, f"/t/{reg.code}?msg=Your+refund+request+was+sent+to+the+organizer.")


# ---- Stripe webhook (no CSRF: authenticated by signature) --------------------
webhook_router = APIRouter()


@webhook_router.post("/stripe/webhook")
async def stripe_webhook(request: Request, session: Session = Depends(get_session)):
    if not settings.stripe_webhook_secret:
        raise HTTPException(404)
    payload = await request.body()
    try:
        kind = payments.handle_webhook(session, payload, request.headers.get("stripe-signature", ""))
    except (ValueError, stripe.SignatureVerificationError):
        return PlainTextResponse("bad signature", status_code=400)
    return PlainTextResponse(kind)


# ---- Fake checkout for development (DEV_FAKE_PAYMENTS=true, no Stripe key) ---

@router.get("/dev/checkout/{code}")
def fake_checkout(code: str, request: Request, session: Session = Depends(get_session),
                  user: User = Depends(auth.require_user)):
    if not settings.dev_fake_payments or settings.stripe_secret_key:
        raise HTTPException(404)
    reg, ev = _ticket_for(session, code, user)
    return render(request, "fake_checkout.html", reg=reg, ev=ev, ticket=session.get(TicketType, reg.ticket_type_id))


@router.post("/dev/checkout/{code}")
def fake_checkout_pay(code: str, request: Request, session: Session = Depends(get_session),
                      user: User = Depends(auth.require_user)):
    if not settings.dev_fake_payments or settings.stripe_secret_key:
        raise HTTPException(404)
    reg, ev = _ticket_for(session, code, user)
    services.confirm_paid(session, reg, None)
    return redirect(request, f"/t/{reg.code}?paid=1")
