# Lanyard

A public website for finding and running LAN parties. Anyone can sign up and host. Organizers get an event page, free or paid tickets, a seat map, polls, tournament signups, announcements and reminder emails. Players find events, register, pick a seat and get a ticket.

`Lanyard` is a working name; set `SITE_NAME` to change it everywhere.

## What's in it

**For players.** Search upcoming LANs by city, venue or state. Sign in with an emailed link (no passwords). Register for a free or paid ticket, choose a seat on the event's map, vote in polls, sign up for tournaments, and keep a ticket with a short code. Free tickets can be cancelled by the holder; paid tickets have a "Request a refund" button that emails the organizer.

**For organizers.** Events start as private drafts. The dashboard has tabs for:

- **Details:** date, time zone, venue, capacity, registration window, public or unlisted, whether to show who's going. The street address only shows to confirmed attendees.
- **Tickets:** any number of ticket types, each free or paid, with optional quantity limits. A ticket type's price locks once someone buys it.
- **Seating:** a rows × columns grid; click cells to turn them into aisles or gaps. With seating on, capacity is the smaller of the event capacity and the seat count.
- **Attendees:** the list, CSV export, comp tickets for staff or cash payers, and cancel-with-refund.
- **Announcements:** posted to the event page and optionally emailed to everyone (capped at 5 emails per event per day).
- **Polls:** single-choice; attendees can change their vote.
- **Tournaments:** signups with optional player caps, a bracket link (Challonge, start.gg, anything), and a "Make teams" button that shuffles signups into even teams and locks signups.

Cancelling an event emails everyone and refunds every paid ticket automatically.

**For you (site admin).** Anyone in `ADMIN_EMAILS` gets `/admin`: approve events over `FREE_CAPACITY_LIMIT` before they can publish, take down events, ban accounts (signs them out and hides their events), and see emails that are failing to send.

**Automatic.** A background worker inside the app sends queued email, releases unpaid checkout holds after 30 minutes, and sends reminders a week and a day before each event.

## Payments (Stripe Connect)

Each organizer connects their own Stripe Express account from **Host a LAN > Payments**. Tickets are charged with Stripe Checkout as destination charges `on_behalf_of` the organizer: the money lands in their account, their name shows on the card statement, and refunds come out of their balance. Set `PLATFORM_FEE_PERCENT` if you want a cut; it's 0 by default.

Starting a paid registration holds the spot for 30 minutes (Stripe's minimum checkout lifetime). Held spots count toward capacity, so the event can't oversell while people are paying. If a payment somehow completes after the hold ran out and the spot was taken, the payment is still honored and the dashboard flags the overage, rather than taking money for a ticket that doesn't exist.

### Setting up Stripe

1. In the Stripe dashboard, turn on **Connect** and choose Express accounts. Fill in your platform profile.
2. Copy your secret key into `STRIPE_SECRET_KEY`. Start with an `sk_test_` key.
3. Add a webhook endpoint at `https://YOUR-SITE/stripe/webhook`, listening to events on **Connected accounts and your account**, with these events:
   - `checkout.session.completed`
   - `checkout.session.async_payment_succeeded`
   - `checkout.session.expired`
   - `account.updated`
   - `charge.refunded`
4. Copy the endpoint's signing secret into `STRIPE_WEBHOOK_SECRET`.
5. In test mode: connect a test organizer account, sell yourself a ticket with card `4242 4242 4242 4242`, then cancel and refund it from the attendee list. Check that the ticket confirms and the refund appears in Stripe. Only then switch to live keys.

For local work without Stripe, `DEV_FAKE_PAYMENTS=true` replaces checkout with a fake "Pay" page. It's ignored whenever a Stripe key is set.

## Running it

### Docker Compose

```bash
cp .env.sample .env       # filled-in example; replace every CHANGE_ME and the example domain
docker compose up -d --build
```

`.env.example` has the same settings with longer explanations. The app refuses to start while any value still contains `CHANGE_ME`. That starts the app and a Postgres 16 container. Tables are created on first start. Put it behind your reverse proxy (Pangolin, Caddy, Traefik...) at the `BASE_URL` address; the site must be served over https in production because session cookies are marked Secure when `BASE_URL` starts with `https://`.

**Behind a reverse proxy:** the app trusts `X-Forwarded-For` from `FORWARDED_ALLOW_IPS` (default `*`). That's correct as long as only your proxy can reach the container's port. If the port is exposed to your LAN or the internet, set it to the proxy's IP, or the sign-in rate limiter can be bypassed by spoofing the header.

**Don't put the whole site behind SSO.** This is a public site; your proxy should pass everything through. If you want `/admin` behind Pocket ID as an extra layer, protect just that path.

### Dockhand or Portainer

Dockhand can't build from `build: .`, so either:

- push this repo to GitHub; `.github/workflows/docker.yml` publishes `ghcr.io/<your-user>/lanyard:latest` on every push to `main`, and the compose file pulls `IMAGE`; or
- build once on the host with `docker build -t ghcr.io/wolfej4/lanyard:latest .`.

Use an absolute `DATA_PATH` (for example `/mnt/user/appdata/lanyard`), since Dockhand runs stacks from its own directory. Enter `SECRET_KEY`, `POSTGRES_PASSWORD` and `STRIPE_*` as plain variables if secret-flagged ones come through as a literal `***`.

### Email

Sign-in depends on email, so nobody can log in until SMTP works. Use a transactional provider (Postmark, Resend, Amazon SES, Mailgun) with SPF and DKIM set up on your sending domain. Home or residential SMTP will land in spam or be blocked outright. With `SMTP_HOST` blank, emails are printed to the container log, which is handy for local testing: `docker compose logs -f app` and click the sign-in link.

### Local development

```bash
pip install -r requirements.txt
export SECRET_KEY=dev BASE_URL=http://localhost:8000 DEV_FAKE_PAYMENTS=true
python -m app.seed          # demo events in ./data/lanyard.db (SQLite)
uvicorn app.main:app --reload
```

Sign in as `organizer@example.com` (the link prints to the terminal) to manage the demo events.

SQLite works for development. Use Postgres for anything public: row locking is what stops two people from grabbing the last spot at the same moment, and SQLite doesn't do it.

### Tests

```bash
DATABASE_URL=postgresql://user:pass@localhost/lanyard_test python -m tests.test_flow
```

**The test drops and recreates every table**, so point it at a throwaway database. It runs 51 checks through the real app: sign-in links (single use), CSRF and cross-origin blocking, drafts staying private, search, free and paid registration, seat conflicts, checkout holds and sell-outs, Stripe webhook signatures and retries, polls, tournament team shuffling, announcements, CSV export, refunds, reminders, event cancellation, large-event approval, bans, and six people racing for the last two spots.

## Configuration

Everything is set with environment variables; `.env.example` documents each one. The ones you must set are `BASE_URL`, `SECRET_KEY`, `POSTGRES_PASSWORD`, and SMTP. For paid tickets add `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET`.

## How it's built

FastAPI, SQLModel on Postgres, Jinja templates with htmx for the interactive bits (seat picking, voting, tournament signups, live search). No build step and no frontend framework. htmx and the Archivo font are bundled locally, so the site loads nothing from third-party servers and the Content Security Policy only allows its own scripts.

```
app/
  main.py          app setup, security headers, error pages
  config.py        environment variables
  models.py        tables
  auth.py          email sign-in, sessions, CSRF, rate limiting
  services.py      registration rules: capacity, holds, confirm, cancel
  payments.py      Stripe Connect onboarding, checkout, refunds, webhook
  jobs.py          background worker: email outbox, holds, reminders
  mailer.py        SMTP and the outbox
  seed.py          demo data
  routes/          public pages, account, registration, polls and tournaments, organizer dashboard, admin
  templates/       pages, partials, and plain-text email templates
  static/          CSS, htmx, fonts
```

Some decisions worth knowing about:

- **Email goes through a database outbox.** A slow or broken SMTP server never blocks a page, nothing is lost on restart, and failures show up on `/admin`.
- **Sign-in links open a confirm page instead of signing you in on click.** Corporate email scanners open links to check them, which would otherwise use up the one-time token before the person clicks.
- **All times are stored in UTC.** Each event has its own time zone, used for entering and showing times, so a Central-time organizer and an Eastern-time player both see the event's local time with its zone.
- **Concurrency is handled in the database.** Registering locks the event row, and partial unique indexes make it impossible to hold two live registrations for one event or for two people to share a seat.

## Terms of Service and Privacy Policy

`/terms` and `/privacy` are linked from the footer, the sign-in page, the registration panel and the Stripe setup page. They fill in your details from `OPERATOR_NAME`, `CONTACT_EMAIL`, `GOVERNING_LAW`, `LEGAL_UPDATED` and `MIN_AGE`; anything unset shows as a bracketed placeholder like `[set CONTACT_EMAIL]` so it's obvious on the live page. The text lives in `app/templates/legal/`. Change `LEGAL_UPDATED` whenever you edit it.

The Terms include the wording Stripe asks Connect platforms to include about the Stripe Connected Account Agreement.

These were written to match what the code actually does, so a few promises in the Privacy Policy depend on how you run the server:

- **Logs are kept up to 30 days.** The compose file caps app logs by size; also set retention on your reverse proxy's access logs (Pangolin/Traefik) so nothing with IP addresses lives longer than 30 days.
- **Backups are kept up to 30 days.** Rotate your `pg_dump`s or volume snapshots to match.
- **Privacy requests are answered within 30 days** at `CONTACT_EMAIL`. Use an inbox someone actually reads.
- **Emails go through a provider.** If yours isn't covered by the "email delivery provider" wording, or you add analytics or any third-party script, update the policy first.

The app enforces the rest itself: sign-in records are purged after 30 days and sent emails after 90 (`jobs.purge_old_records`), public pages never show email addresses, tournament signups follow the event's "show who's going" setting, and people can delete their own account from `/me/delete` once they've cancelled upcoming registrations and events. Deletion anonymizes past registrations rather than removing them, because attendance counts and payment records depend on them.

## Before you open it to the public

- **Have a lawyer read the Terms and Privacy Policy.** They're a solid, specific starting point, not legal advice.
- **Database migrations.** Tables are created with `create_all`, which won't alter existing tables. Add Alembic before your first schema change on a live database.
- **Backups.** Back up the Postgres volume, or schedule `pg_dump`.
- **A real domain and sending reputation.** Set up SPF, DKIM and DMARC for the address in `MAIL_FROM`.
- **More than one app container.** The worker runs inside the app process and the sign-in rate limiter is in memory, both fine for one container. If you scale out, set `RUN_WORKER=false` on all but one replica (the outbox already uses `SKIP LOCKED`, so even two workers won't double-send) and move rate limiting to the proxy or Redis.

## Ideas for later

Waitlists when an event sells out; built-in brackets instead of linking out (the bracket engine from the LAN party suite would port over); distance search using geocoded venues; a check-in mode for the door using ticket codes; organizer co-hosts; recurring events; event images.
