"""Background worker: drains the email outbox, releases expired checkout holds,
and sends event reminders. Runs as an asyncio task inside the web process
(RUN_WORKER=true), or as its own container with `python -m app.jobs`."""
import asyncio
import logging
from datetime import timedelta

from sqlmodel import Session, select

from . import mailer, services
from .config import settings
from .db import engine
from .models import EmailOutbox, Event, Registration, User, utcnow
from .util import fmt_dt

log = logging.getLogger("lanyard.jobs")
MAX_ATTEMPTS = 5


def drain_outbox(batch: int = 25) -> int:
    sent = 0
    with Session(engine) as session:
        q = (select(EmailOutbox)
             .where(EmailOutbox.sent_at.is_(None), EmailOutbox.attempts < MAX_ATTEMPTS)
             .order_by(EmailOutbox.id).limit(batch))
        if settings.is_postgres:
            q = q.with_for_update(skip_locked=True)
        for msg in session.exec(q).all():
            try:
                mailer.send_now(msg.to_addr, msg.subject, msg.body)
                msg.sent_at = utcnow()
                sent += 1
            except Exception as exc:  # noqa: BLE001 - keep draining the rest
                msg.attempts += 1
                msg.last_error = str(exc)[:500]
                log.warning("Email %s to %s failed (attempt %s): %s", msg.id, msg.to_addr, msg.attempts, exc)
            session.add(msg)
        session.commit()
    return sent


def send_reminders() -> int:
    now = utcnow()
    queued = 0
    with Session(engine) as session:
        events = session.exec(select(Event).where(
            Event.status == "published", Event.start_at > now,
            Event.start_at <= now + timedelta(days=7))).all()
        for ev in events:
            soon = ev.start_at <= now + timedelta(days=1)
            if soon and ev.reminder_day_sent:
                continue
            if not soon and ev.reminder_week_sent:
                continue
            # Skip the week reminder for events created inside the final week.
            if not soon and ev.created_at > ev.start_at - timedelta(days=7):
                ev.reminder_week_sent = True
                session.add(ev)
                continue
            regs = session.exec(select(Registration).where(
                Registration.event_id == ev.id, Registration.status == "confirmed")).all()
            for reg in regs:
                user = session.get(User, reg.user_id)
                mailer.queue(session, user.email, "reminder.txt", kind="reminder", event_id=ev.id,
                             event=ev, reg=reg, soon=soon, seat=reg.seat,
                             when=fmt_dt(ev.start_at, ev.timezone))
                queued += 1
            if soon:
                ev.reminder_day_sent = ev.reminder_week_sent = True
            else:
                ev.reminder_week_sent = True
            session.add(ev)
        session.commit()
    return queued


def purge_old_records() -> None:
    """Retention promised in the privacy policy: sign-in tokens go after 30 days,
    sent emails after 90 days."""
    from sqlmodel import delete
    from .models import LoginToken
    now = utcnow()
    with Session(engine) as session:
        session.exec(delete(LoginToken).where(LoginToken.created_at < now - timedelta(days=30)))
        session.exec(delete(EmailOutbox).where(EmailOutbox.sent_at.is_not(None),
                                               EmailOutbox.sent_at < now - timedelta(days=90)))
        session.exec(delete(EmailOutbox).where(EmailOutbox.sent_at.is_(None), EmailOutbox.attempts >= MAX_ATTEMPTS,
                                               EmailOutbox.created_at < now - timedelta(days=90)))
        session.commit()


_last_purge = None


def tick() -> None:
    global _last_purge
    if _last_purge is None or utcnow() - _last_purge > timedelta(hours=6):
        purge_old_records()
        _last_purge = utcnow()
    with Session(engine) as session:
        services.expire_holds(session)
    send_reminders()
    drain_outbox()


async def run_forever(interval: float = 15.0) -> None:
    while True:
        try:
            await asyncio.to_thread(tick)
        except Exception:  # noqa: BLE001
            log.exception("Background tick failed")
        await asyncio.sleep(interval)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    from .db import init_db
    init_db()
    asyncio.run(run_forever())
