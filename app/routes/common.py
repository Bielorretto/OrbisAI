"""Utilitaires partagés par les routes : utilisateur courant, rendu des pages, messages flash."""
from datetime import datetime, timedelta
from pathlib import Path
import hashlib
import hmac
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import clock, config, labels
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


# --------------------------------------------------------------------------- identité par onglet
# Chaque onglet garde sa propre identité : un jeton signé (paramètre « as » ou en-tête X-As) transporté par les
# liens, formulaires et appels de l'onglet (voir static/app.js). On peut ainsi ouvrir associé, collaborateur et
# stagiaire côte à côte. Le cookie « uid » ne sert que de repli quand aucun jeton n'est présent.

def identity_token(user: User) -> str:
    signature = hmac.new(config.SECRET_KEY.encode(), str(user.id).encode(), hashlib.sha256).hexdigest()[:16]
    return f"{user.id}.{signature}"


def _user_id_from_token(token: str) -> int | None:
    uid, _, signature = token.partition(".")
    if not uid.isdigit():
        return None
    expected = hmac.new(config.SECRET_KEY.encode(), uid.encode(), hashlib.sha256).hexdigest()[:16]
    return int(uid) if hmac.compare_digest(signature, expected) else None


def request_token(request: Request) -> str | None:
    return request.headers.get("x-as") or request.query_params.get("as")


def with_params(url: str, **params: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query.update({k: v for k, v in params.items() if v is not None})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request_token(request)
    if token:
        uid = _user_id_from_token(token)
    else:
        cookie = request.cookies.get("uid")
        uid = int(cookie) if cookie and cookie.isdigit() else None
    user = db.get(User, uid) if uid else None
    if user is None:
        raise LoginRequired()
    return user


def require_role(user: User, *roles: str) -> None:
    if user.role not in roles:
        raise Forbidden()


def redirect(url: str, message: str | None = None, error: bool = False) -> RedirectResponse:
    """Redirection ; le message de confirmation passe par l'adresse (propre à l'onglet, pas de cookie partagé)."""
    if message:
        url = with_params(url, flash=message, flash_error="1" if error else None)
    return RedirectResponse(url, status_code=303)


def render(request: Request, template: str, db: Session, user: User | None, **context):
    context.update(
        request=request, user=user, now=clock.now(db), ai_status=ai.status(),
        flash=request.query_params.get("flash"), flash_error=request.query_params.get("flash_error") == "1",
        unread=unread_count(db, user) if user else 0,
        timer=billing.running_timer(db, user) if user else None,
        all_users=db.query(User).order_by(User.role, User.name).all() if user else [],
    )
    return templates.TemplateResponse(request, template, context)


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
