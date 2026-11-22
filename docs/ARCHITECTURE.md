# ChurchPlus architecture

## 1. Tenancy model

One application and one PostgreSQL database serve every church. Each subscribing church
is a **tenant** (`tenants` table). Every church-owned table carries a non-null
`tenant_id` foreign key with `ON DELETE CASCADE`, added by `TenantScopedMixin`.

### Isolation is automatic and fail-closed (`app/utils/tenancy.py`)

| Layer | What it does |
| --- | --- |
| Query filter | A `do_orm_execute` hook adds `tenant_id = <active church>` to every ORM SELECT, UPDATE and DELETE, including joins, subqueries, counts and relationship loads. |
| No church active | Queries on church data raise `TenantContextMissing`; shapes that cannot be inspected (e.g. `count()` over a subquery) match **no** rows. |
| Load check | Every loaded row is re-checked against the active church. |
| Write check | New rows are stamped with the active church. Creating for, editing, deleting or moving a row to another church raises `TenantIsolationError`. |
| Requests | Tenant state is cleared at the start and end of every request. An isolation error is shown as **404**, so other churches' record IDs are never confirmed to exist. |
| Files | Storage keys start with `tenants/<id>/`; reads and deletes from another church are refused. |
| Public IDs | URLs and APIs use UUID `public_id`s, never sequential integers. |

`tenant_context(id)` activates a church. `tenant_bypass()` is for platform code only
(provisioning, migrations, seeding, super-admin aggregates). Entering `tenant_context`
inside a bypass turns isolation back on.

**Rules for developers**

1. Every new church-owned model uses `TenantScopedMixin`.
2. Never use raw SQL (`text()`) for church data — it bypasses the ORM filter.
3. When linking two records (e.g. member → branch), call `assert_same_tenant(a, b)`.
4. Every module adds cross-tenant tests modelled on `tests/test_tenant_isolation.py`.

Planned second layer (Stage 11): PostgreSQL row-level security policies keyed on a
session variable, so the database also enforces isolation.

### Church structures supported

* **Independent church** – one tenant, one or more `branches` (HQ branch created automatically).
* **Multi-branch church** – branches inside one tenant; branch admins are limited to their branch (Stage 3).
* **Denomination** – a `denominations` record; the HQ is a tenant of kind `denomination_hq`,
  member churches are tenants of kind `denomination_member` with `parent_tenant_id` set.
  Headquarters oversight of member churches is an explicit, audited grant (Stage 2/3),
  never an automatic bypass.

## 2. Identity and access

* `users` – global login identity (email unique, scrypt password hash, UUID session id).
* `tenant_users` – a user's account inside one church, with branch and roles.
  One person can belong to several churches.
* `roles` – per church, so each church can edit roles and create custom ones.
  13 default roles are copied in at provisioning.
* `permissions` – global catalogue of `module.action` codes:
  21 modules × view / create / edit / delete / approve / export / manage_settings.
  Prayer, counselling and children are flagged **sensitive**.
* Platform staff (`users.platform_role`: super_admin / support_admin) have **no** church
  roles by default and therefore no access to counselling or confidential member records.

## 3. Data model (Stage 0)

```
denominations 1─* tenants ─┬─* branches
                tenants ◄──┘ (parent_tenant_id)
tenants 1─* roles *─* permissions          (role_permissions)
tenants 1─* tenant_users *─* roles          (tenant_user_roles)
users   1─* tenant_users
tenants 1─* subscriptions *─1 plans
tenants 1─* audit_logs (tenant_id NULL = platform event)
```

All tables have primary keys, timestamps, named constraints (stable migrations) and
indexes on foreign keys and lookup columns. Schema changes are made only through
Alembic migrations; a test checks the migrations produce exactly the model schema.

## 4. Security baseline

* Secrets only in environment variables; production refuses to start without
  `SECRET_KEY` and a PostgreSQL `DATABASE_URL`.
* CSRF protection (Flask-WTF), rate limiting (Flask-Limiter), secure/HttpOnly/SameSite cookies.
* Strict Content-Security-Policy with no inline scripts; X-Frame-Options DENY; nosniff;
  HSTS over HTTPS. `ProxyFix` trusts Render's single proxy hop.
* Uploads: type allow-list, content signature check, size limit, random file names,
  private storage outside `static/`.
* Append-only `audit_logs` with actor, IP and user agent.
* Ghana Data Protection Act, 2012 (Act 843): retention and export policies are built
  with the membership and reporting stages.

## 5. Accounts and sessions (Stage 3)

* Flask-Login's session id is `<public_id>.<session_version>`. Changing or resetting a
  password, or "sign out all other devices", bumps the version, which invalidates every
  other session and remember-me cookie at once.
* Sign-in is rate limited (10/min, 50/hour per IP) and locks an account for 15 minutes
  after 8 failures; every attempt is stored in `login_events`.
* Emailed links (invitation, reset, verification) are signed with `SECRET_KEY`, expire,
  and embed a fingerprint of the current password/verification state so they work once.
* Two-step sign-in uses TOTP (RFC 6238). Recovery codes are stored as scrypt hashes.
  Hardening planned for Stage 11: encrypt `users.totp_secret` at rest with a key held
  outside the database.
* Role changes follow "no privilege escalation" rules (see `app/auth/team.py`).

## 6. Accounting (Stage 6)

* `app/services/ledger.py` is the only code that writes journal entries. Receipts, vouchers,
  payments and transfers all call it, so the books and the finance pages can never disagree.
* Each entry: ≥ 2 lines, debits = credits, lines one-sided and non-negative (also enforced by
  CHECK constraints), amounts `NUMERIC(14,2)` handled as `Decimal`.
* Immutability: there is no edit or delete for posted entries. `ledger.reverse()` posts the
  opposite entry and marks the original void; reports include both, so each period shows
  exactly what was recorded in it.
* Expenses use accrual: approval posts Dr expense / Cr accounts payable; payment posts
  Dr accounts payable / Cr bank. Unpaid approved vouchers therefore appear as liabilities.
* Pledges are memorandum records; only `Contribution` rows (receipts) create income.
* Statements are computed from the ledger on demand; branch filtering uses the branch on each line.

## 7. Confidential ministry data (Stage 7)

All rules live in `app/services/ministry.py`; every list and detail page goes through these
query helpers, so a page cannot forget a rule.

| Data | Who can see it |
|---|---|
| Prayer request (ordinary) | `prayer.view` |
| Prayer request (confidential) | the person assigned, or `prayer.approve`; opening it is audited |
| Counselling case (standard) | its counsellor, or `counselling.approve` (oversight); every view audited |
| Counselling case (restricted) | its counsellor only |
| Counselling session notes | the counsellor only (oversight sees date and status) |
| Children's profiles, guardians, check-ins | `children.view`, limited to the user's branch for branch-scoped roles |
| Safeguarding concerns | `children.approve` |

* Records that are hidden return **404**, not 403, so their existence is not revealed.
* `audit.PRIVATE_PREFIXES` keeps counselling and safeguarding activity out of dashboard feeds;
  the full audit trail (for people with `audit.view`) shows only references, never content.
* Platform (SaaS) administrators are not church accounts and have no church roles, so the
  permission checks above never let them in. Stage 10's admin console will show billing and
  usage only.
* Child check-out compares the pick-up code in constant time, refuses unauthorised guardians,
  records every refusal, and is rate limited.

## 8. Communication, portal, website (Stage 8)
* `app/services/messaging.py`: provider adapters (console/Hubtel/Arkesel SMS, console/Meta
  WhatsApp, email), audiences, `create_message` → `deliver`; background `process_due` and
  `run_daily_reminders` run from Render cron. Automatic messages use a `dedupe_key` (unique per
  church) so a reminder is never sent twice.
* Portal accounts are `TenantUser` rows with `member_id` and only the *member* role. The
  pseudo-permissions `portal` and `staff` drive navigation; plan user limits count staff only.
* The website blueprint loads the church from the URL in a blueprint `before_request`, which runs
  after the app-wide context loader, so the site's church always wins for that request.

## 9. Billing and platform (Stage 10)
* `Invoice` and `BillingPayment` are church-scoped; the platform console reads them with
  `tenant_bypass()` and writes inside `tenant_context(church)` so stamping and audit stay correct.
* `billing.verify()` is the only path to *paid*; it asks the provider. `mark_paid()` is idempotent.
* Suspension for non-payment sets `settings.suspended_reason = "billing"`; `billing_access`
  lets such churches reach only the billing pages.

## 10. Installable app (Stage 11)
* `/manifest.webmanifest` and `/sw.js` (scope `/`) come from `app/core`. The worker caches only
  `/static/` files and the offline page; navigations always go to the network.

## 11. Naming note

The blueprint lists `app/services/` twice (business-logic services and church service
management). To avoid a clash, `app/services/` holds business logic and church service
management will live in `app/worship/` (permission module `worship`).
Configuration lives only in `app/config.py` (no duplicate root `config.py`).
