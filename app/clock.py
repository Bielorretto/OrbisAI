"""Horloge de l'application.

Toute la logique métier passe par `now(db)` plutôt que `datetime.now()` : en démo, on peut
« avancer le temps » pour montrer l'expiration d'une proposition ou la notification J-x
sans attendre.
"""
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models import Setting

_OFFSET_KEY = "clock_offset_seconds"


def get_offset(db: Session) -> timedelta:
    row = db.get(Setting, _OFFSET_KEY)
    return timedelta(seconds=float(row.value)) if row else timedelta()


def now(db: Session) -> datetime:
    return (datetime.now() + get_offset(db)).replace(microsecond=0)


def advance(db: Session, delta: timedelta) -> None:
    row = db.get(Setting, _OFFSET_KEY)
    total = get_offset(db) + delta
    if row:
        row.value = str(total.total_seconds())
    else:
        db.add(Setting(key=_OFFSET_KEY, value=str(total.total_seconds())))
    db.flush()


def reset(db: Session) -> None:
    row = db.get(Setting, _OFFSET_KEY)
    if row:
        db.delete(row)
        db.flush()
