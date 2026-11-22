# ChurchPlus platform console (Kayron staff)

Sign in with a platform account (`flask churchplus create-superadmin`). You land on **/platform**.

| Page | What you can do |
|---|---|
| Overview | Churches by status, monthly recurring revenue, money collected in 30 days, past-due count, churches awaiting approval, trials ending in 7 days, overdue invoices, sign-ups per month. |
| Churches | Search and filter all churches; open one for its plan, subscription dates, usage counts and invoices. |
| Church page | **Approve** (when approval is required), **Suspend** with a reason, **Reactivate**, **Extend trial**, **Change plan** (no invoice), **New invoice** (plan price or an agreed amount), **Record payment** for bank/MoMo transfers, **Void** an invoice. |
| Invoices | Open, paid and void invoices across all churches, and the latest payments. |
| Plans | Prices, limits (staff users, members, branches), trial length, availability. New prices apply to the next invoice. |

**Roles**: *Super admin* can act; *Support admin* can only look.

## What the console never shows
Member names or contacts, giving, prayer, counselling, children or safeguarding records. Usage
is shown as counts. Every platform action is written to that church's audit trail, so the church
can see what ChurchPlus changed.

## Billing cycle
1. Trial ends → an invoice is created; the subscription is *past due*; the church sees a banner.
2. Grace period (`BILLING_GRACE_DAYS`, default 7) → still unpaid → the workspace is **paused**
   (data kept). Owners can still open *Subscription* and pay.
3. Payment through Paystack or Hubtel is **verified with the provider** before the invoice is
   marked paid; the church reopens immediately and the paid period is extended.
4. Renewals follow the same path at the end of each period.

Paystack dashboard → Settings → API Keys & Webhooks: set the webhook URL to
`https://<domain>/webhooks/paystack`. Hubtel's callback URL is sent with each checkout.
