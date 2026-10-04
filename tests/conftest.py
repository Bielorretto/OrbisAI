"""Les tests tournent sur une base SQLite temporaire, en mode IA simulé, sans planificateur."""
import os
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp()) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}"
os.environ["AI_MODE"] = "mock"
os.environ["SCHEDULER_ENABLED"] = "false"

import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import Matter, Task, User  # noqa: E402
from app.seed import reset_database  # noqa: E402


@pytest.fixture()
def db():
    reset_database()
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture()
def people(db):
    return {u.name: u for u in db.scalars(select(User))}


def task_by_title(db, start: str) -> Task:
    return db.scalars(select(Task).where(Task.title.startswith(start))).one()


def matter(db, reference: str) -> Matter:
    return db.scalars(select(Matter).where(Matter.reference == reference)).one()
