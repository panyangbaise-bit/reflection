from alembic import context
from sqlalchemy import create_engine, pool

from portfolio_backend.config import settings
from portfolio_backend.models import Base

config = context.config
target_metadata = Base.metadata

if context.is_offline_mode():
    context.configure(
        url=settings().migration_database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(settings().migration_database_url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
