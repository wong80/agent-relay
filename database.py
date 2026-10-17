"""PostgreSQL database setup and durable Agent Relay models.

This module is intentionally the only place that knows about the database
engine and its transaction boundary.  The rest of the application talks to the
models through :mod:`storage`, which takes PostgreSQL row locks
(``SELECT ... FOR UPDATE`` / ``SKIP LOCKED``) inside the transactions opened
here.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Generator

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker


DEFAULT_DATABASE_URL = "postgresql+psycopg://relay:relay@127.0.0.1:5432/agent_relay"


def _database_url() -> str:
    url = os.getenv("RELAY_DATABASE_URL") or os.getenv("DATABASE_URL") or DEFAULT_DATABASE_URL
    # Accept the common postgres:// and postgresql:// spellings, but always use
    # the psycopg 3 driver that the project depends on.
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg://" + url[len(prefix) :]
            break
    if not url.startswith("postgresql+psycopg://"):
        raise RuntimeError(
            "RELAY_DATABASE_URL must be a PostgreSQL URL, "
            "for example postgresql+psycopg://relay:relay@127.0.0.1:5432/agent_relay"
        )
    return url


def positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


DATABASE_URL = _database_url()
LEASE_SECONDS = positive_int("RELAY_LEASE_SECONDS", 60)
MAX_ATTEMPTS = positive_int("RELAY_MAX_ATTEMPTS", 5)
RECOVERY_INTERVAL_SECONDS = max(1, positive_int("RELAY_RECOVERY_INTERVAL_SECONDS", 5))
MAX_BODY_BYTES = positive_int("RELAY_MAX_BODY_BYTES", 256 * 1024)
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_db_time(value: datetime) -> datetime:
    """Timestamp columns are ``timestamp without time zone`` holding UTC."""

    return value.astimezone(timezone.utc).replace(tzinfo=None)


def db_time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def iso_time(value: datetime | None) -> str | None:
    value = db_time(value)
    if value is None:
        return None
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


class Base(DeclarativeBase):
    pass


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    sent_tasks: Mapped[list[Task]] = relationship(
        "Task", foreign_keys="Task.sender_id", back_populates="sender", passive_deletes=True
    )
    received_tasks: Mapped[list[Task]] = relationship(
        "Task", foreign_keys="Task.recipient_id", back_populates="recipient", passive_deletes=True
    )


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (UniqueConstraint("sender_id", "idempotency_key", name="uq_task_sender_idempotency"),)

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    sender_id: Mapped[str] = mapped_column(String(100), ForeignKey("agents.id"), nullable=False, index=True)
    recipient_id: Mapped[str] = mapped_column(String(100), ForeignKey("agents.id"), nullable=False, index=True)
    input: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    sender: Mapped[Agent] = relationship("Agent", foreign_keys=[sender_id], back_populates="sent_tasks")
    recipient: Mapped[Agent] = relationship("Agent", foreign_keys=[recipient_id], back_populates="received_tasks")
    attempts: Mapped[list[Attempt]] = relationship(
        "Attempt", back_populates="task", cascade="all, delete-orphan", order_by="Attempt.attempt_number"
    )


class Attempt(Base):
    __tablename__ = "attempts"
    __table_args__ = (UniqueConstraint("task_id", "attempt_number", name="uq_attempt_task_number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        String(100), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    claim_token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    claimed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    terminal_action: Mapped[str | None] = mapped_column(String(10), nullable=True)
    terminal_payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    task: Mapped[Task] = relationship("Task", back_populates="attempts")


engine: Engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)

SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False, autoflush=True)


# Arbitrary application-wide key for the schema-creation advisory lock.
SCHEMA_LOCK_KEY = 7_204_191_123


def init_db() -> None:
    """Create missing tables, safely even when several replicas start at once.

    Concurrent ``CREATE TABLE`` statements for the same table race in
    PostgreSQL's catalog and one fails, so each process first takes a
    transaction-scoped advisory lock.  Later processes wait, then find the
    tables already present.
    """

    with engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": SCHEMA_LOCK_KEY})
        Base.metadata.create_all(connection)


@contextmanager
def db_session() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def transaction() -> Generator[Session, None, None]:
    """Run one state-changing operation in a single database transaction.

    Callers lock the rows they decide on before changing them, so concurrent
    API processes reach one consistent outcome: claims use
    ``FOR UPDATE SKIP LOCKED`` so racing workers take different tasks, and
    heartbeat, terminal submission, and recovery lock the task row first.
    Locking the task before its attempts everywhere keeps the lock order
    consistent and avoids deadlocks.
    """

    with SessionLocal() as session:
        with session.begin():
            yield session


def lock_task(db: Session, task_id: str, *, skip_locked: bool = False) -> Task | None:
    """Return ``task_id`` locked for update, or ``None`` if missing (or busy when skipping)."""

    return db.scalar(
        select(Task)
        .where(Task.id == task_id)
        .with_for_update(skip_locked=skip_locked)
        .execution_options(populate_existing=True)
    )


def recover_expired_in_session(db: Session, now: datetime) -> int:
    """Expire active leases and requeue/fail their tasks within ``db``."""

    now_db = as_db_time(now)
    task_ids = db.scalars(
        select(Attempt.task_id)
        .where(Attempt.outcome == "processing", Attempt.lease_expires_at <= now_db)
        .order_by(Attempt.lease_expires_at, Attempt.id)
    ).all()
    count = 0
    for task_id in dict.fromkeys(task_ids):
        # A task another transaction holds (a heartbeat or completion in
        # flight) is skipped; a later pass re-checks it.
        task = lock_task(db, task_id, skip_locked=True)
        if task is None:
            continue
        expired = db.scalars(
            select(Attempt)
            .where(
                Attempt.task_id == task_id,
                Attempt.outcome == "processing",
                Attempt.lease_expires_at <= now_db,
            )
            .execution_options(populate_existing=True)
        ).all()
        for attempt in expired:
            attempt.outcome = "expired"
            attempt.finished_at = now_db
            count += 1
        if expired and task.status == "processing":
            if task.attempt_count >= MAX_ATTEMPTS:
                task.status = "failed"
                task.error = "attempts_exhausted"
                task.output = None
                task.finished_at = now_db
            else:
                task.status = "queued"
                task.finished_at = None
    return count


def recover_expired() -> int:
    """Run one recovery pass and return the number of expired attempts."""

    with transaction() as db:
        return recover_expired_in_session(db, utcnow())


__all__ = [
    "Agent",
    "Attempt",
    "Base",
    "DATABASE_URL",
    "DEFAULT_PAGE_SIZE",
    "LEASE_SECONDS",
    "MAX_ATTEMPTS",
    "MAX_BODY_BYTES",
    "MAX_PAGE_SIZE",
    "RECOVERY_INTERVAL_SECONDS",
    "Task",
    "as_db_time",
    "db_session",
    "db_time",
    "engine",
    "init_db",
    "iso_time",
    "lock_task",
    "recover_expired",
    "recover_expired_in_session",
    "transaction",
    "utcnow",
]
