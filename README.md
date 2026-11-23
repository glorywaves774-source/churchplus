# ChurchPlus

Multi-tenant church management SaaS by **Kayron Global Tech Services**.
Flask · PostgreSQL · SQLAlchemy · Alembic · Gunicorn · Render.

> **Version 1.1.1 – all twelve stages (0–11) complete, plus the Aurora UI.** See `CHANGELOG.md` for what each stage added,
> `docs/USER_GUIDE.md` for churches and `docs/LAUNCH_CHECKLIST.md` before going live.

## Quick start (local development)

```bash
python3 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env                                     # leave DATABASE_URL empty to use SQLite
flask --app run.py db upgrade                            # create the tables
flask --app run.py churchplus seed-plans                 # Starter / Growth / Professional / Enterprise
flask --app run.py churchplus seed-demo                  # two demo churches (development only)
flask --app run.py churchplus create-superadmin --email you@example.com
flask --app run.py run --debug                           # http://127.0.0.1:5000
# Demo sign-ins (password ChangeMe-12345):
#   pastor@gracechapel.test  church owner      abena@gracechapel.test  member portal
#   admin@churchplus.test    ChurchPlus platform console
```

Health check: `GET /healthz` returns `{"status": "ok", "database": "ok", ...}`.

## Tests

```bash
pytest                                     # in-memory SQLite (fast)
TEST_DATABASE_URL=postgresql://user:pass@localhost/churchplus_test pytest   # same suite on PostgreSQL
```

340 tests cover every module: the app factory, configuration safety, security headers, roles
and permissions, migrations, finance and double-entry rules, privacy rules for prayer,
counselling and children, messaging, the portal, the website, reports and exports, billing and
webhooks, the platform console, the installable app, and **cross-church isolation** everywhere.

## Management commands

| Command | Purpose |
| --- | --- |
| `flask churchplus sync-permissions` | Add new permissions from the catalogue (safe to repeat) |
| `flask churchplus seed-plans` | Create the default subscription plans if missing |
| `flask churchplus create-superadmin --email …` | Create or promote a SaaS Super Administrator |
| `flask churchplus seed-demo` | Two demo churches (refused outside development/testing) |
| `flask churchplus send-due` | Send scheduled messages (Render cron, every 5 minutes) |
| `flask churchplus send-reminders` | Birthday greetings and next-day reminders (daily) |
| `flask churchplus billing-run` | ChurchPlus invoices, past-due and pauses (nightly) |
| `flask churchplus encrypt-secrets` | Encrypt any plain two-step keys (runs before each deploy) |
| `flask churchplus export-church --slug … --out f.zip` | One church's data as CSV files |
| `flask db migrate -m "…"` / `flask db upgrade` | Schema changes — always via migrations |

## Project layout

```
app/
  __init__.py        application factory, security headers, error handlers
  config.py          Development / Testing / Production settings (env vars only)
  extensions.py      db, migrate, login, csrf, limiter
  models/            tenants, branches, users, roles, permissions, audit, plans, subscriptions
  services/          business logic: provisioning, permissions, audit, storage
  utils/tenancy.py   the tenant isolation engine
  auth/              sign-in, two-step, passwords, my account, team users and roles
  audit/             audit trail and CSV export
  dashboard/         church dashboard
  members/           members, families, documents, transfers, ID cards, import/export
  visitors/          first-timers, follow-up, conversion, retention
  attendance/        registers, headcounts, check-in, trends, absentees
  worship/           services, service types, order of service, duty rota
  departments/       departments and ministries
  clergy/            clergy, staff, volunteers, employment history, leave
  finance/           giving, receipts, pledges, campaigns, expenses, approvals, transfers, budgets
  accounting/        chart of accounts, journals, statements, cashbook, reconciliation
  events/ sermons/   events and programmes; sermon archive and media library
  prayer/ counselling/ children/   pastoral care and children's ministry (strictest access)
  communication/     SMS, WhatsApp, email, announcements, reminder settings
  portal/            member portal (members' own sign-in)
  website/           public church website /c/<church>
  reports/           reports hub with CSV, Excel and PDF
  billing/           church subscription page, Paystack/Hubtel checkout and webhooks
  platform/          ChurchPlus staff console (churches, invoices, plans)
  tenants/           church profile, branches, denomination member churches
  core/              public pages, global search, /healthz
  design/            component gallery (development only)
  ui/                icons, navigation map, template helpers
  api/               /api/v1
  cli/               flask churchplus …
templates/  static/  migrations/  tests/  docs/
```

The app is installable on phones and computers (manifest, icons, service worker in `core/`).

## Documentation

* `docs/ARCHITECTURE.md` – tenancy model, data model, security decisions, rules for new modules
* `docs/DEPLOYMENT.md` – GitHub and Render deployment, environment variables, backups
* `docs/DESIGN_SYSTEM.md` – components, phone behaviour, how to build and check pages
* `docs/USER_GUIDE.md` – plain-language guide for churches
* `docs/PLATFORM_GUIDE.md` – the ChurchPlus staff console and billing cycle
* `docs/SECURITY.md` – data protection and security measures
* `docs/LAUNCH_CHECKLIST.md` – everything to tick before the first paying church
