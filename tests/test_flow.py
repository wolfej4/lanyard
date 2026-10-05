"""End-to-end flow test. Run against a throwaway database:

    DATABASE_URL=postgresql://lanyard:lanyard@localhost/lanyard_test python -m tests.test_flow
"""
import hashlib
import hmac
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_URL", "sqlite:///./data/test.db")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("BASE_URL", "http://testserver")
os.environ["DEV_FAKE_PAYMENTS"] = "true"
os.environ["RUN_WORKER"] = "false"
os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test"
os.environ["ADMIN_EMAILS"] = "admin@example.com"
os.environ["FREE_CAPACITY_LIMIT"] = "20"

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, SQLModel, select  # noqa: E402

from app import jobs  # noqa: E402
from app.db import engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import EmailOutbox, Event, Registration, User, utcnow  # noqa: E402

SQLModel.metadata.drop_all(engine)
SQLModel.metadata.create_all(engine)

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
passed = 0
import logging; logging.getLogger("httpx").setLevel(logging.WARNING); logging.getLogger("lanyard.mail").setLevel(logging.ERROR)


def ok(cond, label):
    global passed
    assert cond, label
    passed += 1
    print(f"  ok  {label}")


def csrf(client, path="/"):
    m = CSRF_RE.search(client.get(path).text)
    return m.group(1)


def login(email, name=None) -> TestClient:
    from app import auth
    auth._ip_hits.clear()  # every test client shares one IP
    c = TestClient(app, base_url="http://testserver")
    with c:
        pass
    r = c.post("/login", data={"email": email, "next": "/", "csrf_token": csrf(c, "/login")})
    assert "Check your email" in r.text, r.text[:500]
    with Session(engine) as s:
        msg = s.exec(select(EmailOutbox).where(EmailOutbox.to_addr == email, EmailOutbox.kind == "login")
                     .order_by(EmailOutbox.id.desc())).first()
    link = re.search(r"http://testserver(/auth/\S+)", msg.body).group(1)
    page = c.get(link)
    assert "Sign in as" in page.text
    r = c.post(link, data={"csrf_token": CSRF_RE.search(page.text).group(1)}, follow_redirects=False)
    assert r.status_code == 303
    if name:
        c.post("/me/name", data={"display_name": name, "next": "/me", "csrf_token": csrf(c, "/me")})
    return c


def post(c, path, data=None, page="/", **kw):
    data = dict(data or {})
    data["csrf_token"] = csrf(c, page)
    return c.post(path, data=data, **kw)


def dt(days, hour=18):
    d = datetime.now() + timedelta(days=days)
    return d.replace(hour=hour, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")


print("Auth")
org = login("org@example.com", "Jacob")
ok("Jacob" in org.get("/me").text, "magic link sign-in and display name")
with Session(engine) as s:
    tok = s.exec(select(EmailOutbox).where(EmailOutbox.kind == "login")).first()
reuse = org.get(re.search(r"http://testserver(/auth/\S+)", tok.body).group(1))
ok("expired" in reuse.text, "sign-in links work only once")
ok(org.post("/manage/new", data={"title": "x"}).status_code == 403, "POST without CSRF token is rejected")
ok(org.post("/manage/new", data={"title": "x", "csrf_token": csrf(org)},
            headers={"Origin": "https://evil.example"}).status_code == 403, "cross-origin POST is rejected")
anon = TestClient(app, base_url="http://testserver")
r = anon.get("/manage", follow_redirects=False)
ok(r.status_code == 303 and r.headers["location"].startswith("/login?next=/manage"), "dashboard requires sign-in")

print("Organizer")
r = post(org, "/manage/new", {"title": "Panhandle Frag Night", "tagline": "BYOC, CS2 and pizza",
                              "start_at": dt(10), "end_at": dt(11, 12), "timezone": "America/Chicago",
                              "capacity": "4", "city": "Crestview", "region": "FL", "venue_name": "The Garage",
                              "address": "123 Main St", "visibility": "public", "show_attendees": "on",
                              "description": "Bring your rig.\n\nhttps://example.com/rules"},
         page="/manage/new", follow_redirects=False)
ok(r.status_code == 303, "create event draft")
eid = int(re.search(r"/manage/(\d+)", r.headers["location"]).group(1))
with Session(engine) as s:
    slug = s.get(Event, eid).slug
ok(anon.get(f"/e/{slug}").status_code == 404, "drafts are hidden from the public")
bad = post(org, "/manage/new", {"title": "Bad", "start_at": dt(5), "end_at": dt(4), "timezone": "America/Chicago",
                                "capacity": "8"}, page="/manage/new")
ok(bad.status_code == 422 and "after the start" in bad.text, "invalid dates are explained")

mp = f"/manage/{eid}?tab=tickets"
post(org, f"/manage/{eid}/tickets", {"name": "BYOC + pizza", "price": "15", "quantity": "2"}, page=mp)
post(org, f"/manage/{eid}/seating", {"enabled": "on", "rows": "2", "cols": "3"}, page=f"/manage/{eid}?tab=seating")
r = org.post(f"/manage/{eid}/seating/toggle", data={"seat": "A2"}, headers={"X-CSRF-Token": csrf(org)})
ok("Effective capacity: <b>4</b>" in r.text, "seat gap lowers effective capacity (2x3 minus 1 gap, capped at 4)")
post(org, f"/manage/{eid}/polls", {"question": "Pizza?", "options": "Pepperoni\nSupreme\nPepperoni"}, page=mp)
post(org, f"/manage/{eid}/tournaments", {"game": "CS2", "format": "Bo3", "team_size": "2"}, page=mp)
r = post(org, f"/manage/{eid}/status", {"action": "publish"}, page=mp, follow_redirects=True)
ok("Published" in r.text, "publish")
home = anon.get("/")
ok("Panhandle Frag Night" in home.text, "event listed on homepage")
ok("Panhandle" in anon.get("/events?q=crestview", headers={"HX-Request": "true", "HX-Target": "results"}).text,
   "search by city (htmx partial)")
ok("No upcoming LANs match" in anon.get("/events?q=tokyo").text, "search with no results")
ok("123 Main St" not in anon.get(f"/e/{slug}").text, "street address hidden from non-attendees")

print("Registration")
with Session(engine) as s:
    from app.models import TicketType
    tickets = {t.name: t.id for t in s.exec(select(TicketType).where(TicketType.event_id == eid))}
free_id, paid_id = tickets["General admission"], tickets["BYOC + pizza"]

r = anon.post(f"/e/{slug}/register", data={"ticket_type_id": free_id, "csrf_token": csrf(anon, f"/e/{slug}")},
              headers={"Referer": f"http://testserver/e/{slug}"}, follow_redirects=False)
ok(r.status_code == 303 and f"next=/e/{slug}" in r.headers["location"], "anonymous register bounces to sign-in and back")

p1 = login("p1@example.com", "Ava")
r = post(p1, f"/e/{slug}/register", {"ticket_type_id": free_id, "gamer_tag": "AvaFrag"}, page=f"/e/{slug}",
         follow_redirects=True)
ok("You&#39;re in" in r.text or "You're in" in r.text, "free registration confirms immediately")
code1 = re.search(r"/t/([A-Z0-9]{8})", str(r.url)).group(1)
ok("123 Main St" in p1.get(f"/e/{slug}").text, "attendees see the street address")
r = post(p1, f"/e/{slug}/register", {"ticket_type_id": free_id}, page=f"/e/{slug}", follow_redirects=True)
ok("already registered" in r.text, "can't register twice")
r = p1.post(f"/t/{code1}/seat", data={"seat": "A1"}, headers={"X-CSRF-Token": csrf(p1), "HX-Request": "true"})
ok('class="seat mine"' in r.text and 'id="stub-seat" hx-swap-oob' in r.text, "pick a seat (updates stub out of band)")
r = p1.post(f"/t/{code1}/seat", data={"seat": "A2"}, headers={"X-CSRF-Token": csrf(p1)})
ok("doesn&#39;t exist" in r.text or "doesn't exist" in r.text, "can't sit in a gap")

p2 = login("p2@example.com", "Ben")
post(p2, f"/e/{slug}/register", {"ticket_type_id": free_id}, page=f"/e/{slug}")
with Session(engine) as s:
    code2 = s.exec(select(Registration).where(Registration.event_id == eid, Registration.gamer_tag == "")).first().code
r = p2.post(f"/t/{code2}/seat", data={"seat": "A1"}, headers={"X-CSRF-Token": csrf(p2)})
ok("just took A1" in r.text and 'class="seat mine"' not in r.text, "two people can't take the same seat")
ok(p2.get(f"/t/{code1}").status_code == 404, "can't view someone else's ticket")

print("Paid tickets")
p3 = login("p3@example.com", "Cal")
r = post(p3, f"/e/{slug}/register", {"ticket_type_id": paid_id}, page=f"/e/{slug}", follow_redirects=False)
ok(r.headers["location"].startswith("/dev/checkout/"), "paid ticket goes to checkout")
code3 = r.headers["location"].rsplit("/", 1)[1]
ok("Waiting for payment" in p3.get(f"/t/{code3}").text, "unpaid ticket shows as pending with a hold")
p4 = login("p4@example.com", "Dee")
r = post(p4, f"/e/{slug}/register", {"ticket_type_id": paid_id}, page=f"/e/{slug}", follow_redirects=False)
code4 = r.headers["location"].rsplit("/", 1)[1]
ok(code4 != code3, "second paid registration holds the last spot")
p5 = login("p5@example.com", "Eli")
r = p5.get(f"/e/{slug}")
ok("Every spot is taken" in r.text, "pending holds count toward capacity (sold out)")

# Pay p3 via simulated checkout, p4 via a signed Stripe webhook.
r = post(p3, f"/dev/checkout/{code3}", page=f"/dev/checkout/{code3}", follow_redirects=True)
ok("Payment received" in r.text, "simulated checkout confirms")
with Session(engine) as s:
    reg4 = s.exec(select(Registration).where(Registration.code == code4)).one()
payload = json.dumps({"id": "evt_1", "object": "event", "type": "checkout.session.completed",
                      "data": {"object": {"id": "cs_1", "object": "checkout.session", "payment_status": "paid",
                                          "payment_intent": "pi_123", "metadata": {"registration_id": str(reg4.id)}}}})
ts = int(time.time())
sig = hmac.new(b"whsec_test", f"{ts}.{payload}".encode(), hashlib.sha256).hexdigest()
bad = anon.post("/stripe/webhook", content=payload, headers={"Stripe-Signature": f"t={ts},v1=deadbeef"})
ok(bad.status_code == 400, "webhook rejects a bad signature")
r = anon.post("/stripe/webhook", content=payload, headers={"Stripe-Signature": f"t={ts},v1={sig}"})
ok(r.status_code == 200, "webhook accepts a signed event")
r = anon.post("/stripe/webhook", content=payload, headers={"Stripe-Signature": f"t={ts},v1={sig}"})
with Session(engine) as s:
    reg4 = s.exec(select(Registration).where(Registration.code == code4)).one()
    n_conf = len(s.exec(select(EmailOutbox).where(EmailOutbox.kind == "confirmed", EmailOutbox.to_addr == "p4@example.com")).all())
ok(reg4.status == "confirmed" and reg4.stripe_payment_intent == "pi_123" and n_conf == 1,
   "webhook confirms once even when Stripe retries")

print("Holds")
p6 = login("p6@example.com")
post(org, f"/manage/{eid}/details", {"title": "Panhandle Frag Night", "start_at": dt(10), "end_at": dt(11, 12),
     "timezone": "America/Chicago", "capacity": "5", "visibility": "public", "show_attendees": "on",
     "city": "Crestview", "region": "FL", "venue_name": "The Garage", "address": "123 Main St"},
     page=f"/manage/{eid}?tab=details")
r = post(p6, f"/e/{slug}/register", {"ticket_type_id": free_id}, page=f"/e/{slug}", follow_redirects=True)
ok("You" in r.text and "in" in r.text, "raising capacity reopens registration")

print("Engagement")
ev_page = p1.get(f"/e/{slug}").text
opt = re.search(r'name="option_id" value="(\d+)"', ev_page).group(1)
pid = re.search(r'/polls/(\d+)/vote', ev_page).group(1)
r = p1.post(f"/polls/{pid}/vote", data={"option_id": opt}, headers={"X-CSRF-Token": csrf(p1), "HX-Request": "true"})
ok(">1<" in r.text.replace(" ", "") and r.text.count('class="opt') == 2, "vote in poll (duplicate options merged)")
ok(p5.post(f"/polls/{pid}/vote", data={"option_id": opt}, headers={"X-CSRF-Token": csrf(p5)}).status_code == 403,
   "non-attendees can't vote")
tid = re.search(r'/tournaments/(\d+)/join', ev_page).group(1)
for c in (p1, p2, p3):
    c.post(f"/tournaments/{tid}/join", headers={"X-CSRF-Token": csrf(c)})
post(org, f"/manage/{eid}/tournaments/{tid}/teams", page=mp)
r = org.get(f"/manage/{eid}?tab=tournaments")
ok("Team 1" in r.text and "Team 2" in r.text and "Reopen signups" in r.text, "shuffle 3 players into 2 teams and lock signups")
post(org, f"/manage/{eid}/announcements", {"body": "Doors at 5.", "email": "on"}, page=mp)
with Session(engine) as s:
    n = len(s.exec(select(EmailOutbox).where(EmailOutbox.kind == "announcement")).all())
ok(n == 5, "announcement emailed to every confirmed attendee")

print("Attendees and refunds")
csvr = org.get(f"/manage/{eid}/attendees.csv")
ok(csvr.headers["content-type"].startswith("text/csv") and "AvaFrag" in csvr.text and ",A1," in csvr.text, "CSV export")
r = post(p3, f"/t/{code3}/cancel", page=f"/t/{code3}")
ok(r.status_code == 403, "paid tickets can't be self-cancelled")
r = post(p3, f"/t/{code3}/refund-request", {"note": "Got sick"}, page=f"/t/{code3}", follow_redirects=True)
ok("refund request was sent" in r.text, "refund request emails the organizer")
r = post(org, f"/manage/{eid}/attendees/add", {"email": "staff@example.com"}, page=mp, follow_redirects=True)
ok("The event is full" in r.text, "comp tickets still respect capacity")
ok(org.get(f"/manage/{eid}?tab=overview").status_code == 200, "overview renders")
other = login("other@example.com")
ok(other.get(f"/manage/{eid}").status_code == 404, "other users can't open your dashboard")

print("Worker")
with Session(engine) as s:
    ev = s.get(Event, eid)
    ev.start_at = utcnow() + timedelta(hours=20)
    ev.end_at = utcnow() + timedelta(hours=40)
    s.add(ev)
    s.commit()
jobs.send_reminders()
jobs.send_reminders()
with Session(engine) as s:
    n = len(s.exec(select(EmailOutbox).where(EmailOutbox.kind == "reminder")).all())
    confirmed = len(s.exec(select(Registration).where(Registration.event_id == eid, Registration.status == "confirmed")).all())
ok(n == confirmed, f"day-before reminder sent once to each of {confirmed} attendees")
sent = jobs.drain_outbox(batch=500)
ok(sent > 0, f"outbox drained ({sent} emails logged, no SMTP configured)")

print("Cancel event")
r = post(org, f"/manage/{eid}/status", {"action": "cancel"}, page=mp, follow_redirects=True)
with Session(engine) as s:
    statuses = {r.code: r.status for r in s.exec(select(Registration).where(Registration.event_id == eid))}
ok(statuses[code4] == "refunded" and statuses[code1] == "cancelled", "cancelling refunds paid tickets and cancels free ones")
ok("cancelled this event" in anon.get(f"/e/{slug}").text, "cancelled notice on event page")

print("Large events and admin")
r = post(org, "/manage/new", {"title": "Big Summer LAN", "start_at": dt(30), "end_at": dt(31),
         "timezone": "America/New_York", "capacity": "120", "visibility": "public"}, page="/manage/new", follow_redirects=False)
big = int(re.search(r"/manage/(\d+)", r.headers["location"]).group(1))
r = post(org, f"/manage/{big}/status", {"action": "publish"}, page="/manage", follow_redirects=True)
ok("quick review" in r.text, "events over the free limit need review")
admin = login("admin@example.com")
ok("Big Summer LAN" in admin.get("/admin").text, "admin sees the review queue")
post(admin, f"/admin/events/{big}/approve", page="/admin")
r = post(org, f"/manage/{big}/status", {"action": "publish"}, page="/manage", follow_redirects=True)
ok("Published" in r.text, "approved large event publishes")
ok(other.get("/admin").status_code == 404, "admin is hidden from regular users")
with Session(engine) as s:
    from app.models import User
    uid = s.exec(select(User).where(User.email == "other@example.com")).one().id
post(admin, f"/admin/users/{uid}/ban", page="/admin")
ok(other.get("/me", follow_redirects=False).status_code == 303, "banned users are signed out")

print("Race for the last spot")
r = post(org, "/manage/new", {"title": "Tiny LAN", "start_at": dt(3), "end_at": dt(4), "timezone": "America/Chicago",
         "capacity": "2", "visibility": "public"}, page="/manage/new", follow_redirects=False)
tiny = int(re.search(r"/manage/(\d+)", r.headers["location"]).group(1))
post(org, f"/manage/{tiny}/status", {"action": "publish"}, page="/manage")
with Session(engine) as s:
    tslug = s.get(Event, tiny).slug
    tfree = s.exec(select(TicketType).where(TicketType.event_id == tiny)).one().id
racers = [login(f"racer{i}@example.com") for i in range(6)]
tokens = [csrf(c, f"/e/{tslug}") for c in racers]
barrier = threading.Barrier(len(racers))


def go(c, t):
    barrier.wait()
    c.post(f"/e/{tslug}/register", data={"ticket_type_id": tfree, "csrf_token": t})


threads = [threading.Thread(target=go, args=(c, t)) for c, t in zip(racers, tokens)]
[t.start() for t in threads]
[t.join() for t in threads]
with Session(engine) as s:
    n = len(s.exec(select(Registration).where(Registration.event_id == tiny, Registration.status == "confirmed")).all())
ok(n == 2, f"6 people racing for 2 spots: exactly {n} got in")

print("Legal pages and account deletion")
r = anon.get("/terms")
ok(r.status_code == 200 and "Stripe Connected Account Agreement" in r.text and 'id="tickets"' in r.text, "terms page")
r = anon.get("/privacy")
ok(r.status_code == 200 and "lanyard_session" in r.text and 'id="retention"' in r.text, "privacy page")
ok('href="/terms"' in anon.get("/").text and 'href="/privacy"' in anon.get("/login").text, "linked from footer and sign-in")
r = post(p2, "/me/delete", {"confirm": "p2@example.com"}, page="/me/delete", follow_redirects=False)
ok(r.status_code == 303, "delete account with typed confirmation")
with Session(engine) as s:
    gone = s.exec(select(User).where(User.email == "p2@example.com")).first()
ok(gone is None, "registrations to past or cancelled events don't block deletion")
ok(p2.get("/me", follow_redirects=False).status_code == 303, "deleted account is signed out everywhere")
with Session(engine) as s:
    leftover = s.exec(select(Registration).where(Registration.code == code2)).one()
ok(leftover.gamer_tag == "" and leftover.seat is None, "deleted account's registrations are anonymized, not removed")
with Session(engine) as s:
    reg = s.exec(select(Registration).where(Registration.event_id == tiny, Registration.status == "confirmed")).first()
    holder_email = s.get(User, reg.user_id).email
holder = login(holder_email)
r = holder.get("/me/delete")
ok("registered for Tiny LAN" in r.text, "upcoming registrations must be cancelled before deleting")
r = post(holder, "/me/delete", {"confirm": holder_email}, page="/me")
ok(r.status_code == 422, "deletion refused while blocked")
import app.jobs as j  # noqa: E402
with Session(engine) as s:
    s.add(EmailOutbox(to_addr="x@example.com", subject="old", body="b", sent_at=utcnow() - timedelta(days=100)))
    s.commit()
j.purge_old_records()
with Session(engine) as s:
    ok(not s.exec(select(EmailOutbox).where(EmailOutbox.subject == "old")).first(), "sent emails purged after 90 days")

print(f"\nAll {passed} checks passed.")
