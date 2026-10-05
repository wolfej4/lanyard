"""Fill an empty database with demo events so you can click around locally.

    python -m app.seed
"""
import json
import random
from datetime import timedelta

from sqlmodel import Session, select

from .db import engine, init_db
from .models import (Announcement, Event, Poll, PollOption, PollVote, Registration, TicketType, Tournament,
                     TournamentEntry, User, utcnow)
from .util import slugify, ticket_code

TAGS = ["Kestrel", "n0scope", "Brickhouse", "TacoTuesday", "LagSwitch", "PixelPete", "Mossberg", "Quill",
        "Vantablack", "Sproket", "Halcyon", "Dunkaroo", "GigaChadwick", "Ferret", "Ozone", "Tumbleweed"]

EVENTS = [
    ("Panhandle Frag Night 12", "BYOC, 24 seats, CS2 and Halo 3 tournaments, pizza at 8", "Crestview", "FL",
     "Crestview Community Center", 9, 24, True),
    ("Gulf Coast Retro LAN", "CRTs, original Xboxes and a 16-player Halo CE system link", "Fort Walton Beach", "FL",
     "The Arcade Loft", 16, 16, False),
    ("Magic City LAN Weekend", "Two days, 60 seats, gigabit to every desk", "Birmingham", "AL",
     "Southside Event Hall", 30, 60, False),
    ("Garage LAN: Age of Empires II", "Eight players, one garage, way too much Mountain Dew", "Navarre", "FL",
     "Jacob's garage", 4, 8, False),
    ("ATL Overnighter", "Doors at 7pm, games until sunrise", "Atlanta", "GA", "Midtown Makerspace", 23, 40, False),
]


def run() -> None:
    init_db()
    with Session(engine) as s:
        if s.exec(select(Event)).first():
            print("Database already has events; not seeding.")
            return
        org = User(email="organizer@example.com", display_name="Jacob")
        s.add(org)
        players = [User(email=f"{t.lower()}@example.com", display_name=t) for t in TAGS]
        s.add_all(players)
        s.flush()
        now = utcnow().replace(minute=0, second=0, microsecond=0)
        for title, tagline, city, region, venue, days, cap, rich in EVENTS:
            start = (now + timedelta(days=days)).replace(hour=23)
            ev = Event(owner_id=org.id, slug=slugify(title), title=title, tagline=tagline, city=city, region=region,
                       venue_name=venue, address="100 Example St", start_at=start,
                       end_at=start + timedelta(hours=14 if days != 30 else 40), capacity=cap,
                       status="published", large_event_approved=True, timezone="America/Chicago",
                       description=("Bring your rig and come hang out. We supply tables, power, a 48-port switch "
                                    "and a game cache so updates fly.\n\nHouse rules: no cheats, no drama, clean up "
                                    "your cans."),
                       bring_notes="Your PC and monitor, headset, power strip, and a 25 ft ethernet cable.")
            if rich:
                ev.seating_enabled, ev.seat_rows, ev.seat_cols = True, 4, 7
                ev.seat_blocked = json.dumps(["A4", "B4", "C4", "D4"])
            s.add(ev)
            s.flush()
            free = TicketType(event_id=ev.id, name="BYOC seat", price_cents=0)
            paid = TicketType(event_id=ev.id, name="BYOC seat + pizza", price_cents=1500, description="Two slices and a drink")
            s.add_all([free, paid])
            s.flush()
            going = random.sample(players, k=min(len(players), max(3, cap // 2 + random.randint(-3, 3))))
            seats = [f"{r}{c}" for r in "ABCD" for c in range(1, 8) if c != 4]
            random.shuffle(seats)
            for i, p in enumerate(going):
                t = paid if i % 3 == 0 else free
                s.add(Registration(event_id=ev.id, user_id=p.id, ticket_type_id=t.id, code=ticket_code(),
                                   gamer_tag=p.display_name, amount_cents=t.price_cents, status="confirmed",
                                   confirmed_at=now, seat=seats.pop() if rich and i < 14 else None))
            if rich:
                s.add(Announcement(event_id=ev.id, author_id=org.id,
                                   body="Doors open at 5. The switch is in the back corner, so bring a long cable."))
                poll = Poll(event_id=ev.id, question="What pizza are we ordering?")
                s.add(poll)
                s.flush()
                opts = [PollOption(poll_id=poll.id, label=l) for l in ("Pepperoni", "Supreme", "Plain cheese", "Hawaiian")]
                s.add_all(opts)
                s.flush()
                for p in going[:11]:
                    s.add(PollVote(poll_id=poll.id, option_id=random.choice(opts[:3]).id, user_id=p.id))
                t1 = Tournament(event_id=ev.id, game="Counter-Strike 2", format="Single elimination, best of 3",
                                team_size=5, max_entrants=20, starts_at=start + timedelta(hours=2),
                                bracket_url="https://challonge.com/")
                t2 = Tournament(event_id=ev.id, game="Halo 3", format="Free for all, Slayer", team_size=1,
                                starts_at=start + timedelta(hours=5))
                s.add_all([t1, t2])
                s.flush()
                for p in going[:9]:
                    s.add(TournamentEntry(tournament_id=t1.id, user_id=p.id))
                for p in going[3:9]:
                    s.add(TournamentEntry(tournament_id=t2.id, user_id=p.id))
        s.commit()
        print("Seeded demo events. Sign in as organizer@example.com to manage them.")


if __name__ == "__main__":
    run()
