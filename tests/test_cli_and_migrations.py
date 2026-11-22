"""Management commands and database migrations."""
from flask_migrate import upgrade, downgrade
from sqlalchemy import inspect

from app.models import Plan, Tenant, User
from app.utils.tenancy import tenant_bypass


def test_cli_seed_plans_and_permissions(app, db):
    runner = app.test_cli_runner()
    assert "4 added" in runner.invoke(args=["churchplus", "seed-plans"]).output
    assert "0 added" in runner.invoke(args=["churchplus", "seed-plans"]).output
    assert "added" in runner.invoke(args=["churchplus", "sync-permissions"]).output
    with tenant_bypass():
        assert db.session.query(Plan).count() == 4


def test_cli_create_superadmin(app, db):
    runner = app.test_cli_runner()
    res = runner.invoke(args=["churchplus", "create-superadmin", "--email", "Boss@Kayron.test"],
                        env={"SUPERADMIN_PASSWORD": "Very-Strong-Pass-1"})
    assert res.exit_code == 0, res.output
    with tenant_bypass():
        u = db.session.query(User).filter_by(email="boss@kayron.test").one()
    assert u.is_platform_staff and u.check_password("Very-Strong-Pass-1")


def test_cli_seed_demo(app, db):
    runner = app.test_cli_runner()
    res = runner.invoke(args=["churchplus", "seed-demo"])
    assert res.exit_code == 0, res.output
    with tenant_bypass():
        assert db.session.query(Tenant).count() == 2


def test_cli_seed_demo_refused_in_production(app):
    app.config["ENV_NAME"] = "production"
    res = app.test_cli_runner().invoke(args=["churchplus", "seed-demo"])
    assert res.exit_code != 0


def test_migrations_match_models(app, db):
    """Running the migrations from empty must produce exactly the model schema."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    db.drop_all()
    with db.engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
    upgrade()
    with db.engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        diff = compare_metadata(ctx, db.metadata)
    assert diff == [], diff
    tables = set(inspect(db.engine).get_table_names())
    assert {"tenants", "branches", "users", "roles", "permissions", "tenant_users",
            "audit_logs", "plans", "subscriptions", "denominations"} <= tables
    downgrade(revision="base")
    assert set(inspect(db.engine).get_table_names()) <= {"alembic_version"}
