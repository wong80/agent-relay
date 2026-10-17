"""Shared test setup: point the app at a disposable PostgreSQL database.

Tests drop and recreate every table, so they never use ``RELAY_DATABASE_URL``
directly.  They use ``RELAY_TEST_DATABASE_URL`` (default: the
``agent_relay_test`` database on the compose ``postgres`` service) and refuse to
run against a database whose name does not contain ``test``.

Start the database first with ``docker compose up -d postgres``.
"""

from __future__ import annotations

import os
import uuid
from typing import Iterator, NamedTuple

import psycopg
import pytest
from psycopg import sql
from sqlalchemy.engine import URL, make_url

DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://relay:relay@127.0.0.1:5432/agent_relay_test"
TEST_DATABASE_URL = make_url(os.getenv("RELAY_TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL))

if "test" not in (TEST_DATABASE_URL.database or ""):
    raise pytest.UsageError(
        f"Refusing to run tests against database {TEST_DATABASE_URL.database!r}: "
        "RELAY_TEST_DATABASE_URL must name a database containing 'test' (its tables are dropped)."
    )


def libpq_url(url: URL) -> str:
    """Render a SQLAlchemy URL as a plain postgresql:// URI for psycopg."""

    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def _admin_connect() -> psycopg.Connection:
    try:
        return psycopg.connect(libpq_url(TEST_DATABASE_URL.set(database="postgres")), autocommit=True)
    except psycopg.OperationalError as exc:
        raise pytest.UsageError(
            f"Cannot reach PostgreSQL at {TEST_DATABASE_URL.host}:{TEST_DATABASE_URL.port}. "
            f"Start it with `docker compose up -d postgres`.\n{exc}"
        ) from exc


def create_database(name: str) -> None:
    with _admin_connect() as admin:
        if admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone() is None:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


def drop_database(name: str) -> None:
    with _admin_connect() as admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


# The app reads its URL when ``database`` is first imported, so this must run
# before any test module imports ``main``.  Overwrite rather than setdefault:
# a developer's RELAY_DATABASE_URL must never leak into the test run.
create_database(TEST_DATABASE_URL.database)
os.environ["RELAY_DATABASE_URL"] = TEST_DATABASE_URL.render_as_string(hide_password=False)
os.environ.pop("DATABASE_URL", None)


class ScratchDatabase(NamedTuple):
    app_url: str
    """SQLAlchemy URL to hand to the relay (RELAY_DATABASE_URL)."""
    libpq_url: str
    """Plain postgresql:// URI for inspecting the database with psycopg."""


@pytest.fixture
def scratch_database() -> Iterator[ScratchDatabase]:
    """A brand-new, uniquely named database that is dropped afterwards."""

    name = f"{TEST_DATABASE_URL.database}_{uuid.uuid4().hex[:12]}"
    create_database(name)
    url = TEST_DATABASE_URL.set(database=name)
    try:
        yield ScratchDatabase(url.render_as_string(hide_password=False), libpq_url(url))
    finally:
        drop_database(name)
