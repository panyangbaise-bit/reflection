"""Durable catalog sync progress and distinct exchange-traded non-ETF funds."""

import sqlalchemy as sa
from alembic import op

revision = "c803_catalog_coverage"
down_revision = "c702_phone_auth"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "instruments", sa.Column("search_aliases", sa.Text(), nullable=False, server_default="")
    )
    # Initial schema used an unnamed check; find its actual PostgreSQL name.
    checks = sa.inspect(op.get_bind()).get_check_constraints("instruments")
    for check in checks:
        if "asset_class" in check["sqltext"]:
            op.drop_constraint(check["name"], "instruments", type_="check")
    op.create_check_constraint(
        "ck_instruments_asset_class",
        "instruments",
        "asset_class in ('stock','etf','fund','listed_fund','gold')",
    )
    op.create_table(
        "catalog_sync_states",
        sa.Column("kind", sa.String(32), primary_key=True),
        sa.Column("last_success", sa.DateTime(timezone=True)),
        sa.Column("last_attempt", sa.DateTime(timezone=True), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(64)),
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON catalog_sync_states TO pa_control")


def downgrade():
    raise RuntimeError("Preserve listed fund identities; plan a data-preserving rollback")
