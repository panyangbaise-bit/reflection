"""Per-user trading discipline stored in PostgreSQL"""

import sqlalchemy as sa
from alembic import op

revision = "a03_trading_discipline"
down_revision = "59d1b8873290"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "trading_discipline",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.execute("ALTER TABLE trading_discipline ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE trading_discipline FORCE ROW LEVEL SECURITY")
    predicate = (
        "(user_id = NULLIF(current_setting('app.user_id', true), '')::uuid "
        "AND public.pa_user_active(user_id))"
    )
    op.execute(
        f"CREATE POLICY tenant_scope ON trading_discipline TO pa_app "
        f"USING {predicate} WITH CHECK {predicate}"
    )
    op.execute(
        "CREATE POLICY control_scope ON trading_discipline TO pa_control "
        "USING (true) WITH CHECK (true)"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON trading_discipline TO pa_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON trading_discipline TO pa_control")


def downgrade():
    op.execute("DROP POLICY tenant_scope ON trading_discipline")
    op.execute("DROP POLICY control_scope ON trading_discipline")
    op.execute("ALTER TABLE trading_discipline DISABLE ROW LEVEL SECURITY")
    op.drop_table("trading_discipline")
