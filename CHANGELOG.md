# Changelog

## 1.2.4 – Menus no longer hidden behind cards
* The **⋯** menu (for example next to **Edit** on a member's page) could be partly covered by
  the card below it, hiding items such as *Giving statement* and *Download personal data*. Open
  menus now always sit on top, on computers, tablets and phones, in light and dark mode.
* Menus have a solid background again, so the page behind never shows through.


## 1.2.3 – Removing a photo is easy to find
* **Remove photo** now sits right next to **Take photo with camera** on member and clergy/staff
  records, as a normal button (it was a small grey link at the bottom of the photo box).
* It appears as soon as a photo is saved with the camera. Before, it only showed after
  reloading the page.


## 1.2.2 – File storage set-up made clear

Photos couldn't be stored on the live site because S3 storage was switched on but no bucket or
keys had been entered. Now:
* People trying to save a photo or document are told storage isn't set up (instead of a vague
  "try again"), and the server log says exactly which setting is missing.
* The platform console has a **File storage** panel showing whether storage is set up, with a
  **Test storage** button (super admins) that saves, reads back and removes a test file and
  explains any problem in plain words: wrong key, wrong secret, bucket not found, no permission,
  wrong region or address. The same check runs with `flask churchplus check-storage`.
* `/healthz` reports the storage state.
* Keys pasted with stray spaces or line breaks are cleaned. Cloudflare R2, Backblaze B2 and other
  S3-compatible services work out of the box (region `auto`, compatible checksums).
* An S3_ENDPOINT_URL that still holds a placeholder such as `<ACCOUNT_ID>` is caught and
  explained (the platform console and the log say exactly what to paste).
* 16 new tests (394 in total), including real S3 calls against an S3 emulator.


## 1.2.1 – Photos from any phone can be stored

**Fixed** – some photos taken on phones could not be saved:
* **iPhone photos (HEIC/HEIF)** were refused ("JPG, PNG or WEBP only"). They are now accepted
  and converted to JPEG: with *Choose file*, in the camera window, on the plain upload form,
  for leaders, documents, receipts, sermon media and the church logo. If a browser can't preview
  an iPhone photo (Chrome on Android or Windows), the camera window still saves it and ChurchPlus
  converts it.
* **Full-size camera photos** (often 10–15 MB) were over the 10 MB limit. The limit is now 25 MB
  and photos are still shrunk before they are stored (a 14 MB photo is saved at 1200 px).
  A file that is still too large now gives a clear message on the same page instead of an error page.
* **Photos with no file extension** (some Android camera apps call them just "image") were
  refused. A photo is now recognised by its content, not its name. Anything that isn't a picture
  is still refused.

10 new automated tests (378 in total). Pillow-HEIF is added to `requirements.txt` so Render
installs the iPhone photo reader.


## 1.2.0 – Camera for member and leader photos

ChurchPlus now has its own camera for taking passport photos of members and leaders. Photos are
saved straight onto each person's record, with no separate app and no file transfers.

### New
* **Take photo** on every member record and clergy/staff record: a camera button sits on the photo.
  * An oval guide shows where the face goes.
  * Phones can switch between the front and back cameras.
  * Photos are saved in the same passport shape used on profiles and ID cards.
* **Check before saving.** You see the photo first, with a warning if it looks too dark or too
  bright, and can **Retake** it. Saving needs a tick confirming the person agrees to their photo
  being kept.
* **Photo day** (Members → Photo day):
  * Lists everyone who still needs a photo, with a progress bar.
  * Can be filtered to **leaders only** (clergy, staff and department heads), by branch, or by name.
  * After each photo, **Next person** moves on without closing the camera, so a queue after
    service goes quickly.
  * Leaders' roles show on their cards, and clergy or staff who aren't members are listed too.
* **Leader photos.** Clergy and staff records can now have a photo. Before, only members could.
  * Photos appear on the Clergy & staff list.
  * Someone who is both staff and a member has one shared photo.
* **Remove photo**, for when a person asks for it to be deleted.
* **Works without the camera too.** If a device has no camera, the camera is blocked, or the
  browser is old, **Choose file** still works. On phones it offers the phone's own camera or gallery.

### Privacy and quality
* **Every photo is cleaned before it is stored,** whether taken with the camera or uploaded:
  * turned the right way up and shrunk to a sensible size;
  * re-saved without hidden details such as the GPS location, the phone model and the time it
    was taken. Uploads used to keep these.
* **The camera switches off** as soon as the window closes or the app goes to the background.
* **Permissions:**
  * Only people who can edit members can take member photos.
  * Only people who can edit clergy & staff can take leaders' photos.
  * Branch staff see only their branch.
  * Every new or removed photo is recorded in the audit trail, including whether it came from
    the camera or a file.
* After a retake, new photos show immediately, while unchanged photos load faster.
* If file storage can't be reached, saving a photo or document now shows a clear "try again in a
  few minutes" message instead of an error page.

### Behind the scenes
* Pillow (the image library) is now listed in `requirements.txt`, so Render installs it.
* 28 new automated tests (368 in total) on SQLite and PostgreSQL.
* The camera was checked in a browser with a test camera at 320–440px phone widths, on a tablet
  and on a computer, in light and dark mode, and with the keyboard open.


## 1.1.1 – Fixes from a full check

A full check of 1.1.0 covered:
* the test suite on SQLite and PostgreSQL, and the database migrations;
* every page as a visitor, a member, church staff and the platform team, at five phone widths
  and on a computer, in light and dark mode;
* every "add" form, all 37 downloads (CSV, Excel and PDF), installing the app and offline mode;
* a security and dependency scan.

There were no crashes, broken pages or security problems. Three things needed fixing:

* **iPhones no longer zoom in when you tap Search or "Find a page".** Both boxes used text
  slightly too small for Safari, which zooms the whole page in when you tap one. They now use the
  same size as every other field on phones, tablets and touch screens. Computers look the same
  as before.
* **The events calendar is usable on phones.** On a phone each event is only a thin coloured bar,
  far too small to tap and with no name shown. A new "*Month* at a glance" list under the calendar
  now shows every event that month: its date, name and time, and whether it was cancelled. Tap one
  to open it. Computers keep the full calendar as before.
* **Negative amounts stay numbers in spreadsheet downloads.** To protect against harmful formulas,
  downloads add an apostrophe in front of anything that starts with "-". This also turned
  negative amounts, such as a deficit on the income statement or an overdrawn ledger balance, into
  text that Excel couldn't add up. Real numbers are now left alone. Anything that could act as a
  formula is still blocked. The audit-trail download now uses the same protection.

19 new automated tests (340 in total) cover these fixes.


## 1.1.0 – Aurora UI
* New look: a soft, slowly drifting "northern lights" glow (purple, teal, gold, rose and blue)
  behind frosted-glass panels, in light and dark mode, on every page – workspace, member portal,
  platform console, sign-in pages and the church website hero.
* Deep navy menu lit with the same colours; gradient primary buttons; the sign-in card keeps its
  soft neumorphic depth.
* Calm by design: the light stands still on phones (smooth scrolling, battery), when the device
  asks for reduced motion, and the glass becomes solid when reduced transparency is on.
  Printouts stay plain white.
* All in one stylesheet (`static/css/aurora.css`); no scripts, images or outside files.


## 1.0.0 – Stage 11: launch readiness, and the installable app

**Installable app**
* ChurchPlus installs like a normal app on Android, iPhone/iPad and computers: app icons
  (including Android adaptive and Apple home-screen icons), web app manifest with shortcuts,
  full-screen standalone mode (the in-app Back button appears only there), an **Install app**
  button (account menu, sign-in page, member portal) and step-by-step help for iPhone.
* A service worker keeps the app's own styles, scripts, fonts and icons on the device for fast
  start-up and shows a friendly offline page. **No church pages or data are ever cached on the
  device.** Each release refreshes the cache automatically.

**Hardening**
* Two-step sign-in keys are encrypted in the database (migration `0008_stage11` encrypts existing
  keys; `flask churchplus encrypt-secrets` runs before each deploy).
* Every response carries a request reference (`X-Request-ID`), every log line shows it, error pages
  show it to users, slow requests are logged.
* Church owners can download all church data as CSV files (counselling, safeguarding and
  confidential prayer excluded); `flask churchplus export-church` does the same for support.
* Render Blueprint now creates three scheduled jobs (messages every 5 minutes, daily reminders,
  nightly billing), a Key Value store for shared rate limits, and a shared secrets group.
* `scripts/backup.sh` for off-site PostgreSQL backups.
* New guides: USER_GUIDE, PLATFORM_GUIDE, SECURITY, LAUNCH_CHECKLIST; DEPLOYMENT rewritten.

## 0.11.0 – Stage 10: ChurchPlus subscriptions and the platform console
* **Subscription page** for churches: plan, status, trial end, usage against limits, invoices,
  plan changes (checked against limits), printable invoices.
* **Online payment** through **Paystack** (cards, mobile money) and **Hubtel**; ChurchPlus never
  sees card data. Payments are confirmed with the provider (amount and currency checked) before an
  invoice is marked paid. Paystack webhooks are signature-checked; Hubtel callbacks are confirmed
  through Hubtel's status API.
* **Nightly billing run**: invoices when trials end and periods renew, *past due* with a banner,
  then a pause after the grace period. Paused churches keep all data and can still pay; paying
  reopens them at once.
* **Platform console** (/platform) for Kayron staff: overview (MRR, collections, past due, trials
  ending, approvals, sign-ups chart), churches, church page with approve / suspend / reactivate /
  extend trial / change plan / custom invoice / record bank or MoMo payment / void, invoices and
  payments, plan prices and limits. Support admins are read-only. The console shows counts only and
  never member, giving, prayer, counselling or children's records; its actions appear in each
  church's audit trail.

## 0.10.0 – Stage 9: Reports and analytics
* Reports hub with 12 reports: membership summary, member directory, birthdays, first-timers and
  conversion, departments, service attendance, events, children's check-ins, giving by month,
  spending by category, messages sent (financial statements stay under Accounting).
* Period presets or custom dates, branch filter, charts, sortable tables, print, and
  **CSV, Excel and PDF** downloads (formula-safe; downloads are audited). Each report requires the
  permission for its data (giving needs finance access, children's needs children access).

## 0.9.0 – Stage 8: Communication, member portal and church website
* **SMS** (Hubtel or Arkesel), **WhatsApp** (WhatsApp Business Cloud API, opted-in members only)
  and **email** to all members, a branch, department, men/women, age group, event registrants,
  upcoming birthdays or typed numbers; personalisation, preview with SMS part count, scheduling,
  delivery report per person, opt-outs respected, duplicates removed.
* **Announcements** for staff, members or everyone (website).
* **Automatic messages**: birthday greetings, event reminders and counselling appointment
  reminders the day before (no appointment details in the text), each sent once.
* **Member portal**: invite a member by email; they see announcements, events (register and
  cancel), sermons and recordings shared with members, their own giving statement and pledges,
  prayer requests, their family, and can update contact details and message preferences. Portal
  sign-ins are free and cannot open any staff page.
* **Church website** at `/c/<church>`: home with service times, about, announcements, events with
  public registration, sermons and recordings, prayer request and "I'm new" forms (feeding Prayer
  and Visitors), giving instructions; phone menu, dark mode, spam protection.
* Contact preferences on each member record. Migration `0007_stage8`.


## 0.8.0 – Stage 7: Ministry (events, sermons, prayer, counselling, children)

**Added**
* **Events and programmes:** conferences, crusades, harvests, retreats, youth camps and more,
  with date, venue, organising department, branch, livestream link and website visibility.
  Month calendar (works on phones) and list views. Optional registration with a place limit:
  extra people go on a **waiting list** and move up automatically when someone cancels.
  Mark attendance, event committee, event budget, and payment vouchers linked to the event
  so spending is shown against its budget. Upcoming events on the dashboard.
* **Sermons and media library:** title, preacher, date, series, scripture, summary and notes;
  video and audio links; uploads of audio (MP3/M4A/WAV), video (MP4/WEBM), PDF, Word and
  pictures, checked by their real content and stored privately per church; play audio in the
  page; "who can see it" (staff, members, everyone) ready for the member portal and website
  in Stage 8; sermons appear in global search.
* **Prayer requests:** recorded by staff (website and portal requests arrive in Stage 8),
  categories, assignment to a person or prayer team, follow-up notes, status (new, praying,
  answered, closed). **Confidential** requests are seen only by the person assigned and by
  pastors with prayer approval; opening one is recorded.
* **Counselling (strictest access):** cases with references (`CC-00001`), counsellor,
  appointments and private session notes. A case is visible to its counsellor; standard cases
  also to people with counselling oversight; **restricted** cases to the counsellor only.
  **Session notes are shown to the counsellor alone** – oversight sees dates and status.
  Every case opened is written to the audit trail and never shown in dashboard activity.
  ChurchPlus platform staff hold no church roles, so they never see counselling records.
* **Children and youth:** children's profiles for under-18 members (class, school, allergies,
  medical and support notes, photo consent), guardians (taken from the family automatically),
  who may collect, and court-order or restriction notes. Classes by age with teachers whose
  **background check and safeguarding training** must be under three years old to be cleared.
* **Secure check-in:** a 4-digit pick-up code printed on a child label and a parent label;
  check-out needs the matching code and an authorised guardian; wrong codes are refused and
  recorded; optional attendance mark for today's service.
* **Safeguarding concerns** (`SG-00001`) visible only to people with children approval rights,
  with referral tracking (e.g. Social Welfare, DOVVSU).
* Two new default roles: **Counsellor** and **Children and Youth Leader** (15 default roles).
  `flask churchplus sync-permissions` adds them to existing churches without changing
  roles a church has customised.
* Dashboard quick actions for events, prayer and children's check-in.

**Fixed**
* Side-by-side layouts could push a card past the right edge on phones and tablets when it
  held a wide table; the phone checker now also reports cut-off cards.
* The "Go to the home page" button on error pages is now a proper button.

Database migration `0006_stage7`. 39 new tests (247 total).


## 0.7.0 – Stage 6: Financial management and accounting

**Added**
* **Double-entry ledger** under everything: a ready-made chart of accounts for Ghanaian
  churches (cash, bank, mobile money, payables, funds, tithes, offerings, special offerings,
  donations, welfare, project contributions, utilities, salaries, ministry, welfare, missions,
  bank and MoMo charges, denominational remittances…). Every entry must balance; each line is
  a debit or a credit, never negative, rounded to the pesewa; the database also refuses
  invalid lines. Posted entries are never edited or deleted – corrections are reversing
  entries, so the history stays complete.
* **Giving:** receipts with their own numbering (`RCP-000001`) for tithes, offerings,
  donations, welfare and project giving, by member number or name, cash, MoMo (with
  transaction ID), bank transfer, cheque or card, paid into cash, bank or MoMo; quick
  "record another"; service offering entry (several counted lines at once); printable receipt;
  void with a reason (reversing entry, number never reused); yearly giving statements per member.
* **Pledges and campaigns:** pledges are never income; only payments against them are.
  Pledge progress, automatic fulfilment, reopening if a payment is voided; campaigns and
  projects with targets, money raised and pledged.
* **Expenses with approval:** payment vouchers (`PV-000001`) with attachments, department and
  branch; submitted → approved (posted to accounts payable) → paid (from bank, cash or MoMo);
  reject or void with reasons; nobody can approve their own voucher; accountants can record
  but not approve.
* **Transfers** between cash, bank and MoMo, with charges.
* **Budgets** by category and by department, with budget-versus-actual.
* **Reports:** trial balance, income and expenditure statement, statement of financial
  position, cash-flow statement (direct method), cashbook and ledgers with running balances,
  journals; consolidated or per branch; print and CSV.
* **Bank reconciliation:** tick cleared lines against the statement; completes only when the
  difference is zero; uncleared items carry forward.
* Finance overview: month's giving, expenses, surplus, balances, approvals waiting, six-month chart.
* Database migration `0005_stage6`. 21 new tests (208 total).


## 0.6.0 – Stage 5: Church operations management

**Added**
* **Services:** schedule church services, special programmes and departmental meetings;
  service types with usual day and time (five created for every church); theme, preacher,
  worship leader, sermon title, scripture readings, venue, notes; order of service with
  times and who leads each part; duty rota (ushers, readers, media…) by name or membership
  number; printable service report.
* **Attendance:** tick-list register (searchable, saves only the rows shown), headcounts
  (men, women, children, visitors), check-in by membership number, scanned card link or a
  USB/Bluetooth card scanner, and camera QR scanning in browsers that support it; weekly
  trend chart, today's services, absentee list (members not seen in 2–12 weeks), and recent
  attendance on each member's profile. Departmental meeting registers list only that
  department's members.
* **Visitors and first-timers:** quick registration (with "save and add another"), linked to
  the service attended; how they heard, who invited them, wishes and prayer request; a
  follow-up is scheduled automatically two days later; follow-ups by call, visit, SMS,
  WhatsApp or email with assignment, outcomes and overdue tracking; one-click conversion to
  membership that carries their details and past visits across; six-month retention report.
* **Departments and ministries:** the blueprint's eleven ministries created by default; leaders,
  member roles (secretary, treasurer…), meeting schedule, meetings and attendance rate.
* **Clergy, staff and volunteers:** staff numbers, link to the member record, position,
  department, branch, employment type and status, ordination date, Ghana Card and SSNIT
  numbers; employment history (contracts, promotions, transfers, appraisals with ratings,
  training) with attached documents; leave requests counted in working days, overlap checks,
  approval by people with approval rights (never their own), balance of annual leave.
* Visitors and staff in global search; dashboard quick actions for first-timers and services.
* Branch-limited roles see only their branch's services, visitors and staff.
* Database migration `0004_stage5`. 17 new tests (187 total), passing on SQLite and PostgreSQL 16.


## 0.5.0 – Stage 4: Church membership management

**Added**
* Member register with automatic membership numbers per church (e.g. `GCI-000124`;
  prefix configurable in church settings data), never reused, safe under concurrent use.
* Full member record: title, names, gender, date of birth and age, marital status,
  occupation and employer, nationality, hometown, contacts, address with Ghana Post GPS,
  emergency contact, branch, date joined, previous church, baptism details, notes,
  birthday-message preference.
* Members list with search (name, number, phone, email), filters (status, branch, gender,
  baptised), sorting, pagination and figures (total, active, new this month, birthdays).
* Member profile with tabs: overview, family, documents, history.
* Passport photographs and member documents (baptism and marriage certificates, transfer
  letters…) in private storage, type-checked, downloads recorded in the audit trail.
* Status changes with reason and effective date, kept as a history.
* Transfers between branches, transfers out to another church, and transfers in (from the
  previous-church field), all recorded.
* Families: create a household, link members with their role, family page.
* QR-code membership card (printable, card-sized). Scanning opens the record only for
  signed-in staff of the same church; a lost card can be reissued so the old code stops working.
* Import from CSV or Excel with a template, validation report (row-by-row errors and
  duplicate-phone warnings) and a preview before anything is saved; plan member limits apply.
* Export to Excel or CSV following the current filters, protected against formula injection.
* Personal-data download for one member (Data Protection Act, 2012 (Act 843) access
  requests) and permanent erasure that also removes their files.
* Branch-limited roles (e.g. Branch Administrator) see and register members of their own
  branch only.
* Members in global search, dashboard member figures and upcoming birthdays.
* Database migration `0003_stage4`. 26 new tests (170 total), passing on SQLite and PostgreSQL 16.


## 0.4.0 – Stage 3: Authentication and security

**Added**
* Password recovery by email (link expires in 1 hour, works once, signs out other devices);
  the same answer is shown whether or not an email has an account.
* Email verification on registration, with a reminder and "send again" in My account.
* Two-step sign-in with any authenticator app (QR code shown in the page, no outside
  service), 8 single-use recovery codes, regenerate or turn off with your password.
* Lockout after 8 failed attempts in 15 minutes; full sign-in history (successes, wrong
  passwords, wrong codes, blocks, sign-outs) with device and IP shown in My account.
* My account: update your details, change password, sign out all other devices. Ending
  sessions also cancels "keep me signed in" on every other device.
* Team management: invite people by email (new or existing ChurchPlus users), choose
  branch and roles, edit access, suspend/reactivate, remove, resend invitations; plan
  user limits enforced.
* Custom roles with a permission grid (21 areas × 7 actions), branch-limited roles,
  sensitive areas flagged. Built-in roles editable, Church Owner locked.
* No privilege escalation: people can only grant roles or permissions they hold, cannot
  change their own access, and cannot suspend or remove the owner or someone with more
  access; permissions they lack are left untouched when they edit a role.
* Audit trail page with area and date filters, pagination, and CSV export protected
  against spreadsheet formula injection.
* Database migration `0002_stage3` (sign-in history, two-step fields, session versions,
  branch-limited roles), tested from an existing Stage 0–2 PostgreSQL database.
* 26 new automated tests (144 total), passing on SQLite and PostgreSQL 16.


## 0.3.0 – Stage 2: SaaS multi-tenant foundation

**Added**
* Church registration: church details, type (church or denomination headquarters), country
  (sets currency and time zone), plan, owner account with a strong-password rule. New churches
  start a free trial, or wait for approval when `REGISTRATION_REQUIRES_APPROVAL=true`.
* Sign in / sign out (rate limited, same message for unknown email and wrong password,
  session renewed at sign-in, safe return to the page you asked for), church chooser and
  switcher for people who belong to several churches.
* Every request activates only the signed-in person's selected church; a tampered session
  cannot select a church they have no account in. Suspended people and suspended church
  accounts are stopped; pending, suspended or cancelled workspaces show a clear status page.
* Page permissions (`@permission_required("branches.edit")`), and menu items that follow them.
* Dashboard with real figures (branches, team, plan, trial days), recent activity from the
  audit trail, a getting-started checklist and quick actions.
* Church profile: names, motto, contacts, address with Ghana Post GPS, country, currency,
  time zone, brand colours, and a private logo upload.
* Branches: list with search and status filter, add, edit, deactivate/reactivate, delete,
  unique codes, protected headquarters branch, plan branch limits.
* Denominations: headquarters can add member churches and invite each church's administrator
  by email; headquarters sees a summary (status, branches, users) but cannot open member
  churches' records.
* Invitation links: signed, expire after 3 days, work only once.
* Transactional email service (console in development, SMTP in production).
* 32 new automated tests (118 total), including cross-church attempts over real requests.


## 0.2.0 – Stage 1: Premium UI/UX design system

**Added**
* Signed-in app shell: deep navy sidebar with sections and active page, collapsible on
  computers (remembered), top bar with breadcrumbs, global search (⌘K / Ctrl+K),
  notification centre, profile menu and Dark mode / Light mode button.
* Phone layout: ☰ menu that slides in over a dimmed page and closes with ×, a tap outside,
  Escape or the Back button; "Find a page" filter; bottom tab bar (3–4 destinations + Menu);
  Back button inside installed home-screen apps; safe-area support.
* Component library as Jinja macros: page headers, cards, statistic tiles, buttons, badges,
  avatars, profile cards, data tables (sortable, scroll inside their box on phones),
  filter bar, pagination, forms with validation messages, file upload, date fields,
  switches, modals, confirmation dialog, dropdowns, tabs, toasts, activity feed,
  quick actions, progress bars, empty states.
* Charts (line, bar, doughnut) that follow light/dark mode and format currency.
* Sign-in page styles (neumorphism + spatial) ready for Stage 3.
* Global search framework: each module registers a search provider; results respect permissions.
* Navigation map for the whole product; menu items appear only when the page exists and
  the person is allowed to open it.
* Design system gallery at /design/ (development and testing only).
* Inter font, Lucide icons and Chart.js are self-hosted; the security policy now allows only
  ChurchPlus's own origin, with no inline scripts or styles anywhere (enforced by tests).
* Developer tools: `scripts/ui_check.py` (phone widths, both themes, console errors) and
  `scripts/ui_interactions.py` (menu, dialogs, theme). 20 new automated tests (86 total).


## 0.1.0 – Stage 0: Project Foundation and Architecture

**Added**
* Flask application factory with Development, Testing and Production configurations;
  production refuses to start without a secret key and PostgreSQL.
* Database foundation: churches (tenants), denominations, branches, users, church user
  accounts, roles, permissions, audit log, subscription plans and subscriptions.
* Automatic, fail-closed tenant isolation for every query and write, plus per-church
  private file storage (local folder or S3-compatible cloud storage).
* 147 permissions across 21 modules and 13 default church roles; churches can add
  custom roles. Platform staff get no access to church records by default.
* Church provisioning service: headquarters branch, default roles, owner account,
  trial subscription and audit entry in one step.
* First Alembic migration, tested on SQLite and PostgreSQL, up and down.
* Management commands: sync-permissions, seed-plans, create-superadmin, seed-demo.
* `/healthz` health check, `/api/v1` root, security headers (strict CSP, HSTS, etc.),
  friendly error pages that return JSON for the API.
* Public home page with Dark mode / Light mode button that follows the device setting
  until chosen, phone-friendly layout and print-safe styles.
* Render Blueprint (`render.yaml`), Procfile, `.python-version`, `.env.example`.
* 66 automated tests, passing on SQLite and PostgreSQL 16.

**Next: Stage 1 – Premium UI/UX design system** (sidebar, phone menu and tab bar,
dashboard components, tables, forms, charts, dark theme for every component).
