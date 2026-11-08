"""Remove commercial subscriptions, store transactions and entitlements."""

import sqlalchemy as sa
from alembic import op

revision = "c805_remove_commercial"
down_revision = "c804_password_onboarding"
branch_labels = None
depends_on = None

TENANT_PREDICATE = (
    "(user_id = NULLIF(current_setting('app.user_id', true), '')::uuid "
    "AND public.pa_user_active(user_id))"
)


def upgrade():
    op.drop_table("store_transactions")
    op.drop_table("subscription_events")
    op.drop_table("entitlements")


def downgrade():
    # Best-effort structural restore of the pre-removal schema.
    op.create_table(
        "entitlements",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
    )
    op.create_index(op.f("ix_entitlements_user_id"), "entitlements", ["user_id"], unique=False)
    op.execute("ALTER TABLE entitlements ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE entitlements FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_scope ON entitlements TO pa_app USING {TENANT_PREDICATE} "
        f"WITH CHECK {TENANT_PREDICATE}"
    )
    op.execute(
        "CREATE POLICY control_scope ON entitlements TO pa_control USING (true) WITH CHECK (true)"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON entitlements TO pa_control")
    op.execute("GRANT SELECT ON entitlements TO pa_app")

    op.create_table(
        "subscription_events",
        sa.Column("notification_uuid", sa.String(length=100), nullable=False),
        sa.Column("environment", sa.String(length=16), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("notification_uuid"),
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON subscription_events TO pa_control")

    op.create_table(
        "store_transactions",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("environment", sa.String(length=16), nullable=False),
        sa.Column("transaction_id", sa.String(length=100), nullable=False),
        sa.Column("original_transaction_id", sa.String(length=100), nullable=False),
        sa.Column("product_id", sa.String(length=200), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("upgraded", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("environment", "transaction_id"),
    )
    op.create_index(
        op.f("ix_store_transactions_original_transaction_id"),
        "store_transactions",
        ["original_transaction_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_store_transactions_user_id"), "store_transactions", ["user_id"], unique=False
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON store_transactions TO pa_control")
