"""Outgoing email goes through a database outbox so a slow or failing SMTP server
never blocks a request, and nothing is lost on restart. The background worker
(jobs.py) drains it."""
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlmodel import Session

from .config import settings
from .models import EmailOutbox

log = logging.getLogger("lanyard.mail")

_env = Environment(
    loader=FileSystemLoader(__file__.rsplit("/", 1)[0] + "/templates/emails"),
    autoescape=select_autoescape(enabled_extensions=()),  # plain-text emails
    trim_blocks=True, lstrip_blocks=True,
)


def render(template: str, **ctx) -> tuple[str, str]:
    """Templates put the subject on the first line, then a blank line, then the body."""
    text = _env.get_template(template).render(site=settings.site_name, base=settings.base_url, **ctx)
    subject, _, body = text.partition("\n\n")
    return subject.strip(), body.strip() + f"\n\n-- \n{settings.site_name} · {settings.base_url}\n"


def queue(session: Session, to: str, template: str, kind: str = "", event_id=None, **ctx) -> None:
    subject, body = render(template, **ctx)
    session.add(EmailOutbox(to_addr=to, subject=subject[:200], body=body, kind=kind, event_id=event_id))


def send_now(to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    name, addr = parseaddr(settings.mail_from)
    msg["From"] = formataddr((name, addr))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=addr.split("@")[-1] if "@" in addr else None)
    msg.set_content(body)

    if not settings.smtp_host:
        log.warning("SMTP_HOST not set; email to %s logged instead of sent:\n%s\n%s", to, subject, body)
        return
    ctx = ssl.create_default_context()
    if settings.smtp_ssl:
        server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=ctx, timeout=20)
    else:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
    with server:
        if settings.smtp_starttls and not settings.smtp_ssl:
            server.starttls(context=ctx)
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(msg)
