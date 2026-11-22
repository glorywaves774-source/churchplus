# Security and data protection

ChurchPlus holds personal data covered by Ghana's **Data Protection Act, 2012 (Act 843)** and
similar laws elsewhere. This is how it is protected.

## Separation between churches
* Every church-owned table has a `tenant_id`. Queries are filtered automatically
  (`app/utils/tenancy.py`); writes for the wrong church are refused; a query with no church
  selected returns nothing (fail closed). Cross-church access returns 404. Tests cover every module.
* The public website pins each request to the church in its address, even for signed-in users
  of other churches.

## Accounts
* Passwords hashed (Werkzeug scrypt/PBKDF2); lockout after 8 failures in 15 minutes; rate limits
  on sign-in, password reset and public forms.
* Optional two-step sign-in (authenticator app) with single-use hashed recovery codes. The
  authenticator keys are **encrypted at rest** (Fernet; `FIELD_ENCRYPTION_KEY`).
* Changing a password or "sign out other devices" ends every other session and remember-me cookie.
* Roles with 147 fine-grained permissions; nobody can grant permissions they do not hold.

## Sensitive ministry data
* Counselling: counsellor-only notes; restricted cases; every view audited; never in dashboards
  or exports. Appointment reminder texts carry no details.
* Children: guardians, pick-up codes (constant-time check, rate-limited, refusals audited),
  safeguarding concerns limited to approvers.
* Confidential prayer requests: assigned person and approvers only.
* Platform staff hold no church roles and the console only shows counts.

## Web security
* Strict Content Security Policy: scripts, styles and fonts only from ChurchPlus itself; no inline
  scripts or styles; no third-party CDNs. Forms may only post to ChurchPlus and to the Paystack /
  Hubtel checkout pages.
* CSRF protection on every form (webhooks are exempt and verified instead: Paystack HMAC-SHA512
  signature; Hubtel confirmed through its status API). Payment amounts and currency are checked.
* Uploaded files are checked by their content, stored privately per church, served only after a
  permission check. CSV/Excel exports neutralise spreadsheet formulas.
* HSTS, X-Frame-Options DENY, nosniff, strict referrer policy.
* The installable app never caches pages (church data) on devices.

## Payments
No card numbers ever reach ChurchPlus. Paystack and Hubtel collect payments; ChurchPlus stores
only the reference, amount, status and channel. The church's office email (not a person's
sign-in email) is passed to Paystack for receipts.

## Secrets
Only in environment variables (Render environment group). Never in code, the database or zips.

## Reporting a problem
Contact the Kayron Global Tech Services security contact (add your address here before launch). Please do not test against live churches' data.
