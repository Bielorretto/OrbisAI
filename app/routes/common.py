"""Utilitaires partagés par les routes : utilisateur courant, rendu des pages, messages flash."""
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, unquote

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import clock, labels
from app.db import get_db
from app.models import CalendarEvent, Role, User
from app.services import ai, billing
from app.services.notifications import unread_count

templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")
templates.env.globals.update(labels=labels, Role=Role)


def _fmt_dt(value: datetime | None, fmt: str = "%d/%m/%Y %Hh%M") -> str:
    return value.strftime(fmt) if value else "—"


def _money(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f} €".replace(",", " ").replace(".", ",")


def _hours(minutes: int | float) -> str:
    h, m = divmod(int(minutes), 60)
    return f"{h} h {m:02d}" if h else f"{m} min"


_DAYS = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]


def _day(value, with_time: bool = False) -> str:
    """« lun. 06/10 » (les noms de jours de strftime dépendent de la locale du système)."""
    text = f"{_DAYS[value.weekday()]} {value.strftime('%d/%m')}"
    return f"{text} {value.strftime('%Hh%M')}" if with_time else text


templates.env.filters.update(dt=_fmt_dt, money=_money, hm=_hours, day=_day)


class LoginRequired(Exception):
    pass


class Forbidden(Exception):
    pass


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.cookies.get("uid")
    user = db.get(User, int(uid)) if uid and uid.isdigit() else None
    if user is None:
        raise LoginRequired()
    return user


def require_role(user: User, *roles: str) -> None:
    if user.role not in roles:
        raise Forbidden()


def redirect(url: str, message: str | None = None, error: bool = False) -> RedirectResponse:
    response = RedirectResponse(url, status_code=303)
    if message:
        response.set_cookie("flash", quote(("!" if error else "") + message), max_age=30)
    return response


def render(request: Request, template: str, db: Session, user: User | None, **context):
    flash = request.cookies.get("flash")
    flash_message, flash_error = None, False
    if flash:
        flash_message = unquote(flash)
        if flash_message.startswith("!"):
            flash_message, flash_error = flash_message[1:], True
    context.update(
        request=request, user=user, now=clock.now(db), ai_status=ai.status(),
        flash=flash_message, flash_error=flash_error,
        unread=unread_count(db, user) if user else 0,
        timer=billing.running_timer(db, user) if user else None,
        all_users=db.query(User).order_by(User.role, User.name).all() if user else [],
    )
    response = templates.TemplateResponse(request, template, context)
    if flash:
        response.delete_cookie("flash")
    return response


# --------------------------------------------------------------------------- agenda

AGENDA_START_HOUR, AGENDA_END_HOUR = 8, 20


def agenda_days(db: Session, user: User, start: datetime, end: datetime, max_days: int = 14) -> list[dict]:
    """Agenda jour par jour, avec la position de chaque événement sur une frise 8h-20h."""
    first = start.replace(hour=0, minute=0, second=0)
    last = min(end, first + timedelta(days=max_days))
    events = (db.query(CalendarEvent)
              .filter(CalendarEvent.user_id == user.id, CalendarEvent.end > first, CalendarEvent.start < last)
              .order_by(CalendarEvent.start).all())
    span = (AGENDA_END_HOUR - AGENDA_START_HOUR) * 60
    days = []
    day = first
    while day <= last:
        if day.weekday() < 5:
            window_start = day.replace(hour=AGENDA_START_HOUR)
            window_end = day.replace(hour=AGENDA_END_HOUR)
            items = []
            for e in events:
                a, b = max(e.start, window_start), min(e.end, window_end)
                if a >= b:
                    continue
                items.append({
                    "event": e,
                    "left": round(100 * (a - window_start).total_seconds() / 60 / span, 2),
                    "width": round(100 * (b - a).total_seconds() / 60 / span, 2),
                })
            days.append({"date": day.date(), "items": items, "is_deadline": day.date() == end.date()})
        day += timedelta(days=1)
    return days
