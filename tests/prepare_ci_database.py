"""Create isolated CI roles/test database; no operational credentials or database."""

import os
import secrets
from pathlib import Path

import psycopg
from psycopg import sql


def main() -> None:
    directory = Path(os.environ["RUNNER_TEMP"]) / "adf-api-ci-secrets"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    credentials = {
        role: secrets.token_urlsafe(32) for role in ["adf_api", "adf_migrator"]
    }
    with psycopg.connect(
        os.environ["ADF_CI_POSTGRES_URL"], autocommit=True
    ) as connection:
        for role, password in credentials.items():
            connection.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE"
                ).format(sql.Identifier(role), sql.Literal(password))
            )
            path = directory / role
            path.write_text(password, encoding="utf-8")
            path.chmod(0o600)
        connection.execute("CREATE DATABASE adf_api_test OWNER adf_migrator")
        connection.execute("REVOKE ALL ON DATABASE adf_api_test FROM PUBLIC")
        connection.execute("GRANT CONNECT ON DATABASE adf_api_test TO adf_api")
    with psycopg.connect(
        os.environ["ADF_CI_POSTGRES_URL"].rsplit("/", 1)[0] + "/adf_api_test",
        autocommit=True,
    ) as connection:
        connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        for schema in ["control", "runtime", "audit", "checkpoints"]:
            connection.execute(
                sql.SQL("CREATE SCHEMA {} AUTHORIZATION adf_migrator").format(
                    sql.Identifier(schema)
                )
            )
        connection.execute("GRANT USAGE ON SCHEMA control, runtime, audit TO adf_api")
    with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as output:
        output.write(
            "ADF_CI_MIGRATOR_PASSWORD_FILE=" + str(directory / "adf_migrator") + "\n"
        )
        output.write("ADF_CI_API_PASSWORD_FILE=" + str(directory / "adf_api") + "\n")


if __name__ == "__main__":
    main()
