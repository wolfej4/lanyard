"""Data model.

Datetimes are timezone-aware UTC. Each event carries its own IANA timezone,
which is used only for input and display.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, Index, Text, UniqueConstraint, text
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True, max_length=254)
    display_name: str = Field(default="", max_length=60)
    is_admin: bool = False
    banned: bool = False
    session_version: int = 1  # bump to sign out everywhere
    stripe_account_id: Optional[str] = Field(default=None, max_length=64)
    stripe_charges_enabled: bool = False
    deleted_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def name(self) -> str:
        # Never fall back to the email address: names show on public pages.
        if self.deleted_at:
            return "Deleted account"
        return self.display_name or f"Player {self.id}"


class LoginToken(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    token_hash: str = Field(index=True, unique=True, max_length=64)
    email: str = Field(index=True, max_length=254)
    next_url: str = Field(default="/", max_length=500)
    expires_at: datetime
    used_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utcnow)


class Event(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    owner_id: int = Field(foreign_key="user.id", index=True)
    slug: str = Field(index=True, unique=True, max_length=90)
    title: str = Field(max_length=120)
    tagline: str = Field(default="", max_length=160)
    description: str = Field(default="", sa_column=Column(Text, nullable=False, server_default=""))
    bring_notes: str = Field(default="", sa_column=Column(Text, nullable=False, server_default=""))
    venue_name: str = Field(default="", max_length=120)
    address: str = Field(default="", max_length=200)
    city: str = Field(default="", max_length=80, index=True)
    region: str = Field(default="", max_length=80, index=True)
    country: str = Field(default="US", max_length=2)
    timezone: str = Field(default="America/Chicago", max_length=64)
    start_at: datetime = Field(index=True)
    end_at: datetime
    capacity: int = 16
    visibility: str = Field(default="public", max_length=10)  # public | unlisted
    status: str = Field(default="draft", max_length=10, index=True)  # draft | published | cancelled
    registration_opens_at: Optional[datetime] = None
    registration_closes_at: Optional[datetime] = None
    show_attendees: bool = True
    large_event_approved: bool = False
    seating_enabled: bool = False
    seat_rows: int = 4
    seat_cols: int = 8
    seat_blocked: str = Field(default="[]", sa_column=Column(Text, nullable=False, server_default="[]"))
    reminder_week_sent: bool = False
    reminder_day_sent: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class TicketType(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    event_id: int = Field(foreign_key="event.id", index=True)
    name: str = Field(max_length=60)
    description: str = Field(default="", max_length=200)
    price_cents: int = 0
    quantity: Optional[int] = None  # None = limited only by event capacity
    active: bool = True
    sort: int = 0


ACTIVE_STATUSES = ("pending", "confirmed")


class Registration(SQLModel, table=True):
    __table_args__ = (
        # One live registration per person per event; one live registration per seat.
        Index(
            "uq_registration_user_live", "event_id", "user_id", unique=True,
            postgresql_where=text("status IN ('pending','confirmed')"),
            sqlite_where=text("status IN ('pending','confirmed')"),
        ),
        Index(
            "uq_registration_seat_live", "event_id", "seat", unique=True,
            postgresql_where=text("seat IS NOT NULL AND status IN ('pending','confirmed')"),
            sqlite_where=text("seat IS NOT NULL AND status IN ('pending','confirmed')"),
        ),
    )
    id: Optional[int] = Field(default=None, primary_key=True)
    event_id: int = Field(foreign_key="event.id", index=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    ticket_type_id: int = Field(foreign_key="tickettype.id")
    code: str = Field(index=True, unique=True, max_length=12)
    status: str = Field(default="pending", max_length=12, index=True)  # pending|confirmed|cancelled|refunded
    gamer_tag: str = Field(default="", max_length=40)
    amount_cents: int = 0
    seat: Optional[str] = Field(default=None, max_length=8)
    hold_expires_at: Optional[datetime] = None
    stripe_session_id: Optional[str] = Field(default=None, max_length=255, index=True)
    stripe_payment_intent: Optional[str] = Field(default=None, max_length=255, index=True)
    created_at: datetime = Field(default_factory=utcnow)
    confirmed_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None


class Announcement(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    event_id: int = Field(foreign_key="event.id", index=True)
    author_id: int = Field(foreign_key="user.id")
    body: str = Field(sa_column=Column(Text, nullable=False))
    emailed: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class Poll(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    event_id: int = Field(foreign_key="event.id", index=True)
    question: str = Field(max_length=200)
    closed: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class PollOption(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    poll_id: int = Field(foreign_key="poll.id", index=True)
    label: str = Field(max_length=100)


class PollVote(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("poll_id", "user_id"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    poll_id: int = Field(foreign_key="poll.id", index=True)
    option_id: int = Field(foreign_key="polloption.id")
    user_id: int = Field(foreign_key="user.id")


class Tournament(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    event_id: int = Field(foreign_key="event.id", index=True)
    game: str = Field(max_length=80)
    format: str = Field(default="", max_length=80)
    team_size: int = 1
    max_entrants: Optional[int] = None
    starts_at: Optional[datetime] = None
    bracket_url: str = Field(default="", max_length=300)
    signups_open: bool = True
    created_at: datetime = Field(default_factory=utcnow)


class TournamentEntry(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("tournament_id", "user_id"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    tournament_id: int = Field(foreign_key="tournament.id", index=True)
    user_id: int = Field(foreign_key="user.id")
    team_no: Optional[int] = None
    created_at: datetime = Field(default_factory=utcnow)


class EmailOutbox(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    to_addr: str = Field(max_length=254)
    subject: str = Field(max_length=200)
    body: str = Field(sa_column=Column(Text, nullable=False))
    kind: str = Field(default="", max_length=30)
    event_id: Optional[int] = Field(default=None, index=True)
    attempts: int = 0
    last_error: str = Field(default="", max_length=500)
    sent_at: Optional[datetime] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utcnow)
