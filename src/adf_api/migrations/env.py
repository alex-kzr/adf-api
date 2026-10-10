"""Migration connection is privileged independently from API and worker roles."""

from alembic import context
from sqlalchemy import create_engine, text

from adf_api.config import Settings
from adf_api.models import Base

engine = create_engine(Settings.from_environment().database_url, pool_pre_ping=True)
if engine.dialect.name == "sqlite":
    engine = engine.execution_options(
        schema_translate_map={"control": None, "runtime": None, "audit": None}
    )
with engine.connect() as connection:
    if engine.dialect.name == "postgresql":
        connection.execute(text("SELECT pg_advisory_lock(184739012)"))
        connection.commit()
    try:
        context.configure(
            connection=connection, target_metadata=Base.metadata, include_schemas=True
        )
        with context.begin_transaction():
            context.run_migrations()
    finally:
        if engine.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_unlock(184739012)"))
            connection.commit()
