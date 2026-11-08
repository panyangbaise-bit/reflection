"""Phone identities, preserving existing user IDs and Apple migration records."""

import sqlalchemy as sa
from alembic import op

revision = "c702_phone_auth"
down_revision = "b61e02d4089a"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("users", "apple_subject", existing_type=sa.String(255), nullable=True)
    op.add_column("users", sa.Column("phone", sa.String(16), nullable=True))
    op.add_column("users", sa.Column("password_hash", sa.String(255), nullable=True))
    op.create_unique_constraint("uq_users_phone", "users", ["phone"])


def downgrade():
    # A downgrade must never delete phone-only users or make their identities unusable.
    raise RuntimeError("Phone identity migration requires an explicit data-preserving rollback")
