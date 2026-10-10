"""Run the frozen Alembic migration chain with the configured migrator credentials."""

from pathlib import Path

from alembic import command
from alembic.config import Config


def main() -> None:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    command.upgrade(config, "head")


if __name__ == "__main__":
    main()
