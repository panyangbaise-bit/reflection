"""Tenant row security and separate runtime roles. Roles are provisioned before migration."""

from alembic import op

revision = "a02_security"
down_revision = "4dcd9a276c2d"
branch_labels = None
depends_on = None

PRIVATE = [
    "consents",
    "devices",
    "entitlements",
    "idempotency_keys",
    "outbox_events",
    "portfolios",
    "preferences",
    "reports",
    "accounts",
    "conversations",
    "ai_runs",
    "messages",
    "positions",
    "ledger_events",
]


def upgrade():
    op.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    op.execute("GRANT USAGE ON SCHEMA public TO pa_app, pa_control")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO pa_control")
    op.execute("REVOKE ALL ON alembic_version FROM pa_control")
    op.execute("""
        CREATE FUNCTION public.pa_user_active(uid uuid) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public
        AS $$ SELECT EXISTS(SELECT 1 FROM public.users WHERE id = uid AND status = 'active') $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.pa_user_active(uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION public.pa_user_active(uuid) TO pa_app")
    for name in PRIVATE:
        op.execute(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY")
        predicate = "(user_id = NULLIF(current_setting('app.user_id', true), '')::uuid AND public.pa_user_active(user_id))"
        op.execute(
            f"CREATE POLICY tenant_scope ON {name} TO pa_app USING {predicate} WITH CHECK {predicate}"
        )
        op.execute(
            f"CREATE POLICY control_scope ON {name} TO pa_control USING (true) WITH CHECK (true)"
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {name} TO pa_app")
    op.execute("REVOKE UPDATE, DELETE ON ledger_events FROM pa_app")
    op.execute("REVOKE INSERT, UPDATE, DELETE ON entitlements FROM pa_app")
    op.execute("GRANT SELECT ON instruments, latest_quotes, fx_rates TO pa_app")


def downgrade():
    for name in PRIVATE:
        op.execute(f"DROP POLICY tenant_scope ON {name}")
        op.execute(f"DROP POLICY control_scope ON {name}")
        op.execute(f"ALTER TABLE {name} DISABLE ROW LEVEL SECURITY")
    op.execute("DROP FUNCTION public.pa_user_active(uuid)")
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM pa_app, pa_control")
