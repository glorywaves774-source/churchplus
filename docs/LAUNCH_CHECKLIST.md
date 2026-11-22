# Launch checklist

Tick each line before inviting the first paying church.

## Accounts and services
- [ ] Render Blueprint deployed (web, 3 cron jobs, PostgreSQL, Key Value) – `/healthz` returns `ok`.
- [ ] Custom domain with HTTPS; `PUBLIC_BASE_URL` set.
- [ ] `FIELD_ENCRYPTION_KEY` generated and saved in the company password manager.
- [ ] S3/R2 bucket: private, versioning on, keys in Render.
- [ ] SMTP sender domain verified (SPF, DKIM); a test invitation arrives in Gmail and Outlook.
- [ ] SMS provider account funded; each church's **sender ID registered** (Hubtel / Arkesel).
- [ ] WhatsApp Business number verified (optional) and message templates approved for campaigns.
- [ ] Paystack live keys; webhook `https://<domain>/webhooks/paystack` added; GHS enabled.
- [ ] Hubtel merchant account live (optional).
- [ ] Super admin created; support staff created as *Support admin*.
- [ ] Plan prices checked under /platform/plans.

## Safety
- [ ] Database backups enabled and **one restore tested**; `scripts/backup.sh` weekly copy scheduled.
- [ ] Uptime monitor on `/healthz`; Render notifications on for failed deploys and cron jobs.
- [ ] Privacy notice and terms of service published; data processing agreement ready for churches.
- [ ] Registered with the Data Protection Commission (Ghana) as a data processor.

## Product checks (real devices)
- [ ] Sign up a test church end-to-end on a real Android phone and a real iPhone (Safari).
- [ ] Install the app on both; open it from the home screen; Back button visible in the app.
- [ ] Dark mode on both; keyboard open on forms; tables scroll inside their boxes.
- [ ] Record giving, approve and pay a voucher; trial balance balances.
- [ ] Child check-in and check-out with the printed labels.
- [ ] Send one real SMS, WhatsApp and email.
- [ ] Pay a real GHS 1 invoice through Paystack (then refund it).
- [ ] Member portal invitation received and accepted.
- [ ] Church website on a phone; prayer and "I'm new" forms reach the church.

## Day one
- [ ] Watch Render logs and the /platform overview for the first week.
- [ ] Keep `REGISTRATION_REQUIRES_APPROVAL=true` until support is ready for open sign-ups.
