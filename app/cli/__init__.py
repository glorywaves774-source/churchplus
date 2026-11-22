"""Management commands: ``flask churchplus <command>``."""
from __future__ import annotations

from decimal import Decimal

import click
from flask import Flask
from flask.cli import AppGroup

from app.extensions import db
from app.models import Plan, PlatformRole, Tenant, User
from app.utils.tenancy import tenant_bypass, tenant_context

cli = AppGroup("churchplus", help="ChurchPlus management commands.")

ALL_MODULES = ["members", "visitors", "attendance", "worship", "departments", "clergy", "finance",
               "accounting", "events", "sermons", "prayer", "counselling", "children",
               "communication", "reports", "website"]

# Proposed starting prices for market testing (blueprint section 7). Editable by the SaaS admin.
DEFAULT_PLANS = [
    dict(code="starter", name="Starter", description="Small churches", sort_order=1,
         monthly_price=Decimal("100.00"), annual_price=Decimal("1000.00"),
         max_users=5, max_members=300, max_branches=1, max_storage_mb=2048,
         modules=["members", "visitors", "attendance", "worship", "finance", "events", "communication", "reports"]),
    dict(code="growth", name="Growth", description="Growing churches", sort_order=2,
         monthly_price=Decimal("250.00"), annual_price=Decimal("2500.00"),
         max_users=15, max_members=1500, max_branches=3, max_storage_mb=10240,
         modules=[m for m in ALL_MODULES if m not in ("accounting", "counselling")]),
    dict(code="professional", name="Professional", description="Established churches", sort_order=3,
         monthly_price=Decimal("500.00"), annual_price=Decimal("5000.00"),
         max_users=50, max_members=10000, max_branches=10, max_storage_mb=51200, modules=ALL_MODULES),
    dict(code="enterprise", name="Enterprise", description="Denominations (custom pricing)", sort_order=4,
         monthly_price=None, annual_price=None, max_users=None, max_members=None,
         max_branches=None, max_storage_mb=None, modules=ALL_MODULES, is_public=False),
]


def seed_plans() -> int:
    added = 0
    for spec in DEFAULT_PLANS:
        if db.session.query(Plan).filter_by(code=spec["code"]).first():
            continue
        db.session.add(Plan(currency="GHS", **spec))
        added += 1
    db.session.flush()
    return added


@cli.command("sync-permissions")
def sync_permissions_cmd():
    """Add any new permissions from the catalogue (safe to repeat)."""
    from app.services.permissions import sync_permissions
    with tenant_bypass():
        n = sync_permissions()
        db.session.commit()
        from app.models import Tenant
        from app.services.permissions import ensure_default_roles
        tenant_ids = [t.id for t in db.session.query(Tenant)]
    roles = 0
    for tid in tenant_ids:
        with tenant_context(tid):
            roles += ensure_default_roles()
            db.session.commit()
    click.echo(f"Permissions synced ({n} added). Default roles added to existing churches: {roles}.")


@cli.command("seed-plans")
def seed_plans_cmd():
    """Create the default subscription plans if missing."""
    with tenant_bypass():
        n = seed_plans()
        db.session.commit()
    click.echo(f"Plans seeded ({n} added).")


@cli.command("create-superadmin")
@click.option("--email", required=True)
@click.option("--first-name", default="Platform")
@click.option("--last-name", default="Administrator")
@click.password_option(envvar="SUPERADMIN_PASSWORD")
def create_superadmin(email, first_name, last_name, password):
    """Create (or promote) a SaaS Super Administrator."""
    email = email.strip().lower()
    with tenant_bypass():
        user = db.session.query(User).filter_by(email=email).one_or_none()
        if user is None:
            user = User(email=email, first_name=first_name, last_name=last_name)
            db.session.add(user)
        user.set_password(password)
        user.platform_role = PlatformRole.SUPER_ADMIN
        db.session.commit()
    click.echo(f"Super administrator ready: {email}")


DEMO_PEOPLE = [
    ("Abena", "Mensah", "female", "1988-03-14"), ("Kwame", "Owusu-Ansah", "male", "1979-07-02"),
    ("Efua", "Boateng", "female", "1995-11-23"), ("Yaw", "Asante", "male", "1966-01-09"),
    ("Akosua", "Darko", "female", "2001-05-30"), ("Kofi", "Adjei", "male", "1990-09-17"),
    ("Ama", "Serwaa", "female", "1984-12-05"), ("Kojo", "Antwi", "male", "1972-04-21"),
    ("Adwoa", "Frimpong", "female", "1998-08-08"), ("Kwabena", "Ofori", "male", "1987-02-27"),
    ("Esi", "Agyeman", "female", "1975-06-11"), ("Kweku", "Appiah", "male", "1993-10-03"),
]


def _seed_demo_operations(tenant):
    """A few weeks of services with attendance so dashboards have something to show."""
    import random
    from datetime import date, timedelta
    from app.models import AttendanceRecord, Member, Service, ServiceKind, ServiceStatus, ServiceType
    from app.utils.tenancy import tenant_context
    rnd = random.Random(7)
    with tenant_context(tenant.id):
        if db.session.query(Service).count():
            return
        st = db.session.query(ServiceType).filter_by(name="Sunday First Service").first()
        members = db.session.query(Member).all()
        sunday = date.today() - timedelta(days=(date.today().weekday() + 1) % 7)
        for w in range(10, -1, -1):
            d = sunday - timedelta(weeks=w)
            s = Service(title="Sunday First Service", service_date=d, service_type_id=st.id if st else None,
                        start_time=st.start_time if st else None, preacher="Rev. Daniel Amoah",
                        status=ServiceStatus.COMPLETED if d <= date.today() else ServiceStatus.SCHEDULED,
                        headcount_men=rnd.randint(60, 90), headcount_women=rnd.randint(90, 130),
                        headcount_children=rnd.randint(30, 50), headcount_visitors=rnd.randint(2, 9))
            db.session.add(s)
            db.session.flush()
            for m in rnd.sample(members, k=min(len(members), rnd.randint(6, 10))):
                db.session.add(AttendanceRecord(service_id=s.id, member_id=m.id))
        db.session.add(Service(title="Thanksgiving Service", service_date=sunday + timedelta(weeks=1), kind=ServiceKind.SPECIAL,
                               theme="Ebenezer – thus far the Lord has helped us", preacher="Rev. Daniel Amoah"))
        db.session.flush()


def _seed_demo_finance(tenant, owner):
    """Six months of giving and spending so finance pages have something to show (demo only)."""
    import random
    from datetime import date, timedelta
    from app.models import Contribution, EntrySource, Member, PaymentMethod
    from app.services import finance as fin, ledger
    from app.utils.tenancy import tenant_context
    rnd = random.Random(11)
    with tenant_context(tenant.id):
        if db.session.query(Contribution).count():
            return
        ledger.ensure_chart()
        cash, bank, momo = ledger.account("1000"), ledger.account("1010"), ledger.account("1020")
        ledger.post(date.today() - timedelta(days=200), "Opening balances", [
            ledger.Line(bank, debit=ledger.money("48000")), ledger.Line(cash, debit=ledger.money("1500")),
            ledger.Line(ledger.account("3000"), credit=ledger.money("49500"))], source=EntrySource.OPENING)
        members = db.session.query(Member).all()
        tithe, offering = ledger.account("4000"), ledger.account("4010")
        for w in range(26, -1, -1):
            d = date.today() - timedelta(weeks=w)
            for m in rnd.sample(members, k=min(5, len(members))):
                fin.record_contribution(received_on=d, amount=rnd.choice([50, 100, 150, 200, 300, 500]), income_account=tithe,
                                        deposit_account=rnd.choice([bank, momo]), method=PaymentMethod.CASH, member=m)
            fin.record_contribution(received_on=d, amount=rnd.randint(1800, 3200), income_account=offering,
                                    deposit_account=cash, method=PaymentMethod.CASH, giver_name="Sunday offering")
        for w in range(24, -1, -4):
            d = date.today() - timedelta(weeks=w)
            for code, payee, amt in (("5010", "ECG", rnd.randint(600, 900)), ("5000", "Staff salaries", 6500),
                                     ("5040", "Children's ministry", rnd.randint(200, 600))):
                e = fin.create_expense(expense_date=d, payee=payee, description=f"{payee} {d:%B}",
                                       expense_account=ledger.account(code), amount=amt, actor=None)
                approver = type("A", (), {"id": owner.id})()
                fin.approve_expense(e, approver)
                fin.pay_expense(e, paid_on=d, payment_account=bank, method=PaymentMethod.BANK_TRANSFER, reference=None,
                                actor=approver)
        db.session.flush()


def _seed_demo_members(tenant):
    from datetime import date
    from app.models import Gender, Member
    from app.services import members as member_svc
    from app.utils.tenancy import tenant_context
    with tenant_context(tenant.id):
        if db.session.query(Member).count():
            return
        for i, (first, last, gender, dob) in enumerate(DEMO_PEOPLE):
            member_svc.create_member(tenant, None, first_name=first, last_name=last, gender=Gender(gender),
                                     date_of_birth=date.fromisoformat(dob), phone=f"024{5550100 + i:07d}",
                                     city="Accra", is_baptised=i % 3 != 0, date_joined=date(2015 + i % 10, 1 + i % 12, 1))


def _seed_demo_ministry(tenant):
    """Events, sermons, prayer requests and a Sunday school (demo only)."""
    from datetime import date, datetime, time, timedelta
    from app.models import (ChildClass, ChildGuardian, ChildProfile, Event, EventStatus, Family, FamilyRole, Gender,
                            Member, PrayerRequest, Sermon, Visibility)
    from app.services import members as member_svc, ministry
    from app.utils.tenancy import tenant_context
    with tenant_context(tenant.id):
        if db.session.query(Event).count():
            return
        today = date.today()
        for days, title, cat, venue, cap in ((6, "Youth Camp 2026", "Youth", "Akosombo Retreat Centre", 80),
                                             (13, "Harvest and Thanksgiving", "Harvest", "Main auditorium", None),
                                             (20, "Couples' Dinner", "Fellowship", "Church hall", 40),
                                             (-12, "Leadership Retreat", "Conference", "Peduase Lodge", 30)):
            e = Event(title=title, category=cat, venue=venue, capacity=cap, requires_registration=cap is not None,
                      starts_at=datetime.combine(today + timedelta(days=days), time(9 if cat != "Fellowship" else 18)),
                      status=EventStatus.PUBLISHED if days > 0 else EventStatus.COMPLETED, is_public=days > 0)
            db.session.add(e)
            db.session.flush()
            if cap and days > 0:
                for m in db.session.query(Member).limit(6):
                    ministry.register(e, member=m, guests=1 if cat == "Fellowship" else 0)
        for w, (title, scripture, series) in enumerate((("Faith that works", "James 2:14-26", "Faith in Action"),
                                                        ("Ebenezer", "1 Samuel 7:12", None),
                                                        ("Taming the tongue", "James 3:1-12", "Faith in Action"),
                                                        ("Ask for wisdom", "James 1:5-8", "Faith in Action"))):
            db.session.add(Sermon(title=title, scripture=scripture, series=series, preacher="Rev. Daniel Amoah",
                                  preached_on=today - timedelta(days=(today.weekday() + 1) % 7 + 7 * w),
                                  summary="Notes and recording from Sunday's message.", visibility=Visibility.PUBLIC))
        db.session.add_all([
            PrayerRequest(name="Visitor", request="Pray for my mother's surgery on Thursday.", category="Healing"),
            PrayerRequest(member_id=db.session.query(Member.id).first()[0], request="Travelling to Kumasi for a new job.",
                          category="Work and career"),
            PrayerRequest(name="Anonymous", request="A family matter.", category="Family", is_confidential=True),
        ])
        nursery = ChildClass(name="Nursery", min_age=0, max_age=3, room="Room 1")
        beginners = ChildClass(name="Beginners", min_age=4, max_age=8, room="Room 2")
        juniors = ChildClass(name="Juniors", min_age=9, max_age=12, room="Room 3")
        youth = ChildClass(name="Teens' Church", min_age=13, max_age=17, room="Annex", is_youth=True)
        db.session.add_all([nursery, beginners, juniors, youth])
        mother = db.session.query(Member).filter_by(first_name="Abena").first()
        fam = Family(name="Mensah family")
        db.session.add(fam)
        db.session.flush()
        if mother:
            mother.family_id, mother.family_role = fam.id, FamilyRole.HEAD
        for first, gender, age in (("Nana", "female", 6), ("Kobby", "male", 10), ("Afia", "female", 14)):
            child = member_svc.create_member(tenant, None, first_name=first, last_name="Mensah", gender=Gender(gender),
                                             date_of_birth=today.replace(year=today.year - age, day=1),
                                             family_id=fam.id, family_role=FamilyRole.CHILD)
            p = ChildProfile(member_id=child.id, class_id=ministry.class_for_age(age).id,
                             allergies="Peanuts" if first == "Nana" else None, photo_consent=first != "Afia")
            db.session.add(p)
            db.session.flush()
            if mother:
                db.session.add(ChildGuardian(child_id=p.id, member_id=mother.id, relationship_to_child="Mother", is_primary=True))
        db.session.flush()


def _seed_demo_engage(tenant):
    """Announcements, a live website, a member-portal sign-in and a sent message (demo only)."""
    from datetime import date
    from sqlalchemy.orm.attributes import flag_modified
    from app.models import Announcement, Audience, Channel, Member, Role, TenantUser
    from app.services import messaging
    from app.utils.tenancy import tenant_context
    with tenant_context(tenant.id):
        if db.session.query(Announcement).count():
            return
        db.session.add_all([
            Announcement(title="21 days of prayer and fasting", body="Join us every evening at 6pm from 1 December.",
                         audience=Audience.PUBLIC, starts_on=date.today(), is_pinned=True),
            Announcement(title="Leaders' meeting", body="All heads of department, Saturday 10am in the church hall.",
                         audience=Audience.STAFF, starts_on=date.today()),
            Announcement(title="Welfare dues", body="Please bring your welfare contributions up to date before Christmas.",
                         audience=Audience.MEMBERS, starts_on=date.today()),
        ])
        t = db.session.merge(tenant)
        data = dict(t.settings or {})
        data["website"] = {"enabled": True, "headline": "A family of faith in the heart of Accra",
                           "about": "We are a Bible-believing church that loves God and serves our community.",
                           "giving_text": "MTN MoMo: 024 555 0000 (Grace Chapel)\nBank: GCB Bank, Ring Road branch, account 1234567890",
                           "show_events": True, "show_sermons": True, "show_service_times": True,
                           "visitor_form": True, "prayer_form": True}
        t.settings = data
        flag_modified(t, "settings")
        t.phone, t.email, t.address_line1 = t.phone or "030 255 0000", t.email or "office@gracechapel.test", t.address_line1 or "12 Ring Road East"
        abena = db.session.query(Member).filter_by(first_name="Abena").first()
        if abena and t.name.startswith("Grace"):
            abena.email = "abena@gracechapel.test"
            user = db.session.query(User).filter_by(email="abena@gracechapel.test").first()
            if user is None:
                user = User(email="abena@gracechapel.test", first_name="Abena", last_name="Mensah")
                user.set_password("ChangeMe-12345")
                with tenant_bypass():
                    db.session.add(user)
                    db.session.flush()
            role = db.session.query(Role).filter_by(slug="member").first()
            db.session.add(TenantUser(user_id=user.id, member_id=abena.id, roles=[role] if role else []))
        msg = messaging.create_message(tenant=t, channel=Channel.SMS, audience={"kind": "all"},
                                       body="Hello {first_name}, join us this Sunday for Harvest! Service starts 9am. – {church}")
        messaging.deliver(msg, t)
        db.session.flush()


@cli.command("seed-demo")
def seed_demo():
    """Create two demo churches for local development (never run in production)."""
    from flask import current_app
    from app.services.provisioning import provision_church

    if current_app.config.get("ENV_NAME") not in ("development", "testing"):
        raise click.ClickException("seed-demo is only for development.")
    with tenant_bypass():
        seed_plans()
        created = []
        for name, email in [("Grace Chapel International", "pastor@gracechapel.test"),
                            ("Living Word Assembly", "admin@livingword.test")]:
            if db.session.query(Tenant).filter_by(name=name).first():
                continue
            owner = User(email=email, first_name="Demo", last_name="Owner")
            owner.set_password("ChangeMe-12345")
            t = provision_church(name=name, owner=owner, plan_code="growth", city="Accra")
            _seed_demo_members(t)
            _seed_demo_operations(t)
            _seed_demo_finance(t, owner)
            _seed_demo_ministry(t)
            _seed_demo_engage(t)
            created.append(name)
        if not db.session.query(User).filter_by(email="admin@churchplus.test").first():
            admin = User(email="admin@churchplus.test", first_name="Platform", last_name="Admin",
                         platform_role=PlatformRole.SUPER_ADMIN)
            admin.set_password("ChangeMe-12345")
            db.session.add(admin)
        db.session.commit()
    click.echo("Demo churches: " + (", ".join(created) or "already present"))
    click.echo("Sign-ins (password ChangeMe-12345): pastor@gracechapel.test (church), "
               "abena@gracechapel.test (member portal), admin@churchplus.test (ChurchPlus platform)")


@cli.command("send-due")
def send_due_cmd():
    """Send scheduled messages whose time has come (run every few minutes)."""
    from app.services import messaging
    click.echo(f"Messages sent: {messaging.process_due()}")


@cli.command("send-reminders")
def send_reminders_cmd():
    """Queue today's birthday greetings and tomorrow's reminders, then send them (run daily)."""
    from app.services import messaging
    queued = messaging.run_daily_reminders()
    sent = messaging.process_due()
    click.echo(f"Reminder messages queued: {queued}; sent: {sent}")


@cli.command("billing-run")
def billing_run_cmd():
    """Create invoices for ended trials and renewals, mark past due, pause unpaid churches (run daily)."""
    from app.services import billing
    stats = billing.billing_run()
    click.echo("Billing run: " + ", ".join(f"{k} {v}" for k, v in stats.items()))


@cli.command("encrypt-secrets")
def encrypt_secrets_cmd():
    """Encrypt any two-step sign-in keys still stored in plain text (safe to repeat)."""
    from app.utils.crypto import encrypt, is_encrypted
    n = 0
    with tenant_bypass():
        for u in db.session.query(User).filter(User.totp_secret_stored.isnot(None)):
            if not is_encrypted(u.totp_secret_stored):
                u.totp_secret_stored = encrypt(u.totp_secret_stored)
                n += 1
        db.session.commit()
    click.echo(f"Secrets encrypted: {n}")


@cli.command("export-church")
@click.option("--slug", required=True, help="The church's workspace ID")
@click.option("--out", "out_path", required=True, type=click.Path(dir_okay=False), help="ZIP file to write")
def export_church_cmd(slug, out_path):
    """Write a church's data export (CSV files in a ZIP), e.g. for a data request."""
    from app.services.export import church_zip
    with tenant_bypass():
        t = db.session.query(Tenant).filter_by(slug=slug).first()
    if t is None:
        raise click.ClickException("No church with that workspace ID.")
    with tenant_context(t.id):
        data = church_zip(t)
    with open(out_path, "wb") as f:
        f.write(data)
    click.echo(f"Exported {t.name} to {out_path} ({len(data)} bytes)")


def register_cli(app: Flask) -> None:
    app.cli.add_command(cli)
