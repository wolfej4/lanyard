"""Stripe Connect (Express accounts, destination charges).

Each organizer connects their own Stripe account. Ticket money is charged by the
platform `on_behalf_of` the organizer and transferred to them automatically, minus
PLATFORM_FEE_PERCENT (0 by default). The organizer is the settlement merchant, so
their name appears on the attendee's card statement.
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import stripe
from sqlmodel import Session, select

from . import services
from .config import settings
from .models import Event, Registration, TicketType, User

log = logging.getLogger("lanyard.payments")
stripe.api_key = settings.stripe_secret_key or None


def live() -> bool:
    return bool(settings.stripe_secret_key)


# ---- Organizer onboarding --------------------------------------------------

def onboarding_url(session: Session, user: User) -> str:
    if not user.stripe_account_id:
        acct = stripe.Account.create(
            type="express",
            email=user.email,
            capabilities={"card_payments": {"requested": True}, "transfers": {"requested": True}},
            business_profile={"product_description": "LAN party tickets"},
            metadata={"user_id": str(user.id)},
        )
        user.stripe_account_id = acct.id
        session.add(user)
        session.commit()
    link = stripe.AccountLink.create(
        account=user.stripe_account_id,
        refresh_url=f"{settings.base_url}/manage/payments/connect",
        return_url=f"{settings.base_url}/manage/payments/return",
        type="account_onboarding",
    )
    return link.url


def dashboard_url(user: User) -> Optional[str]:
    if not user.stripe_account_id:
        return None
    try:
        return stripe.Account.create_login_link(user.stripe_account_id).url
    except stripe.StripeError:
        return None


def refresh_account(session: Session, user: User) -> None:
    if not user.stripe_account_id:
        return
    acct = stripe.Account.retrieve(user.stripe_account_id)
    user.stripe_charges_enabled = bool(acct.charges_enabled)
    session.add(user)
    session.commit()


# ---- Checkout --------------------------------------------------------------

def checkout_url(session: Session, reg: Registration) -> str:
    event = session.get(Event, reg.event_id)
    ticket = session.get(TicketType, reg.ticket_type_id)
    organizer = session.get(User, event.owner_id)
    buyer = session.get(User, reg.user_id)

    if not live():  # DEV_FAKE_PAYMENTS
        return f"/dev/checkout/{reg.code}"

    if reg.stripe_session_id:
        existing = stripe.checkout.Session.retrieve(reg.stripe_session_id)
        if existing.status == "open":
            return existing.url

    fee = round(reg.amount_cents * settings.platform_fee_percent / 100)
    # Stripe requires a checkout to stay open at least 30 minutes, so a resumed
    # checkout extends the hold to match.
    floor = datetime.now(timezone.utc) + timedelta(minutes=31)
    if reg.hold_expires_at < floor:
        reg.hold_expires_at = floor
        session.add(reg)
        session.commit()
    expires = int(reg.hold_expires_at.timestamp())
    params = dict(
        mode="payment",
        line_items=[{
            "quantity": 1,
            "price_data": {
                "currency": settings.currency,
                "unit_amount": reg.amount_cents,
                "product_data": {"name": f"{event.title}: {ticket.name}"[:250]},
            },
        }],
        customer_email=buyer.email,
        client_reference_id=str(reg.id),
        metadata={"registration_id": str(reg.id), "event_id": str(event.id)},
        payment_intent_data={
            "on_behalf_of": organizer.stripe_account_id,
            "transfer_data": {"destination": organizer.stripe_account_id},
            "metadata": {"registration_id": str(reg.id)},
            **({"application_fee_amount": fee} if fee else {}),
        },
        expires_at=expires,
        success_url=f"{settings.base_url}/t/{reg.code}?paid=1",
        cancel_url=f"{settings.base_url}/e/{event.slug}?checkout=cancelled",
    )
    cs = stripe.checkout.Session.create(**params, idempotency_key=f"checkout-{reg.id}-{expires}")
    reg.stripe_session_id = cs.id
    session.add(reg)
    session.commit()
    return cs.url


def refund(reg: Registration) -> None:
    if not live():
        return
    stripe.Refund.create(
        payment_intent=reg.stripe_payment_intent,
        reverse_transfer=True,          # pull the money back from the organizer
        refund_application_fee=True,    # and give back the platform fee
        metadata={"registration_id": str(reg.id)},
        idempotency_key=f"refund-{reg.id}",
    )


# ---- Webhook ---------------------------------------------------------------

def handle_webhook(session: Session, payload: bytes, signature: str) -> str:
    stripe.Webhook.construct_event(payload, signature, settings.stripe_webhook_secret)  # verifies signature
    event = json.loads(payload)  # plain dicts are easier to work with than StripeObjects
    kind = event["type"]
    obj = event["data"]["object"]

    if kind in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        if obj.get("payment_status") == "paid":
            reg = _reg_from_session(session, obj)
            if reg:
                services.confirm_paid(session, reg, obj.get("payment_intent"))
    elif kind == "checkout.session.expired":
        reg = _reg_from_session(session, obj)
        if reg and reg.status == "pending":
            reg.status = "cancelled"
            reg.cancelled_at = datetime.now(timezone.utc)
            reg.seat = None
            session.add(reg)
            session.commit()
    elif kind == "account.updated":
        user = session.exec(select(User).where(User.stripe_account_id == obj.get("id"))).first()
        if user:
            user.stripe_charges_enabled = bool(obj.get("charges_enabled"))
            session.add(user)
            session.commit()
    elif kind == "charge.refunded":
        pi = obj.get("payment_intent")
        reg = session.exec(select(Registration).where(Registration.stripe_payment_intent == pi)).first()
        if reg and obj.get("refunded") and reg.status != "refunded":
            # Refunded from the Stripe dashboard rather than from Lanyard.
            reg.status = "refunded"
            reg.seat = None
            session.add(reg)
            session.commit()
    return kind


def _reg_from_session(session: Session, obj) -> Optional[Registration]:
    rid = (obj.get("metadata") or {}).get("registration_id") or obj.get("client_reference_id")
    if not rid:
        return None
    reg = session.get(Registration, int(rid))
    if reg is None:
        log.warning("Webhook for unknown registration %s", rid)
    return reg
