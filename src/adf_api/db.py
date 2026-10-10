"""One SQLAlchemy session per request; PostgreSQL is the deployment authority."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .errors import DomainError


class Database:
    def __init__(self, url: str) -> None:
        kwargs: dict[str, Any] = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs.update(
                connect_args={"check_same_thread": False}, poolclass=StaticPool
            )
        self.engine = create_engine(url, **kwargs)
        if self.engine.dialect.name == "sqlite":
            self.engine = self.engine.execution_options(
                schema_translate_map={"control": None, "runtime": None, "audit": None}
            )

            @event.listens_for(self.engine, "connect")
            def foreign_keys(connection: Any, _record: Any) -> None:
                connection.execute("PRAGMA foreign_keys=ON")

        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        with self.sessions() as session:
            try:
                yield session
                session.commit()
            except DomainError as exc:
                if exc.persist:
                    session.commit()
                else:
                    session.rollback()
                raise
            except Exception:
                session.rollback()
                raise
