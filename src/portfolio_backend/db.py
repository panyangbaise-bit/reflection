from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from portfolio_backend.config import Settings


def now() -> datetime:
    return datetime.now(UTC)


class Database:
    def __init__(self, config: Settings):
        self.engine = create_engine(
            config.database_url,
            hide_parameters=True,
            pool_pre_ping=True,
            pool_size=config.database_pool_size,
            max_overflow=config.database_max_overflow,
            connect_args={
                "options": (
                    f"-c statement_timeout={config.database_statement_timeout_ms} -c lock_timeout=5000"
                )
            },
        )
        self.control_engine = create_engine(
            config.control_database_url,
            hide_parameters=True,
            pool_pre_ping=True,
            pool_size=config.control_database_pool_size,
            max_overflow=config.database_max_overflow,
            connect_args={
                "options": (
                    f"-c statement_timeout={config.database_statement_timeout_ms} -c lock_timeout=5000"
                )
            },
        )

    @contextmanager
    def tenant(self, user_id: UUID):
        # SET LOCAL is cleared by commit/rollback, including on pooled connections.
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            session.execute(
                text("SELECT set_config('app.user_id', :uid, true)"), {"uid": str(user_id)}
            )
            yield session

    @contextmanager
    def control(self):
        with Session(self.control_engine, expire_on_commit=False) as session, session.begin():
            yield session

    def verify_roles(self):
        for engine, expected in ((self.engine, "pa_app"), (self.control_engine, "pa_control")):
            with engine.connect() as conn:
                role = conn.execute(
                    text(
                        "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
                    )
                ).one()
                if role.rolsuper or role.rolbypassrls or role.rolname != expected:
                    raise RuntimeError(f"Database connection requires restricted role {expected}")

    def close(self):
        self.engine.dispose()
        self.control_engine.dispose()
