"""Historique des tâches, notifications dans l'app et emails."""
import logging
import smtplib
from email.message import EmailMessage

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock, config
from app.models import EmailOutbox, Matter, Notification, Task, TaskEvent, User

log = logging.getLogger("loickaton.notifications")


def link_to(item: Matter | Task | None) -> str:
    if isinstance(item, Matter):
        return f"/matters/{item.id}"
    if isinstance(item, Task):
        return f"/tasks/{item.id}"
    return "/"


def log_event(db: Session, item: Matter | Task, kind: str, message: str, actor: User | None = None) -> TaskEvent:
    """Historique d'un dossier ou d'une tâche (un événement de tâche apparaît aussi dans la frise du dossier)."""
    task_id = item.id if isinstance(item, Task) else None
    matter_id = item.matter_id if isinstance(item, Task) else item.id
    event = TaskEvent(matter_id=matter_id, task_id=task_id, actor_id=actor.id if actor else None, kind=kind,
                      message=message, created_at=clock.now(db))
    db.add(event)
    db.flush()
    return event


def notify(db: Session, user: User, kind: str, message: str, item: Matter | Task | None = None,
           link: str | None = None, email_subject: str | None = None) -> Notification:
    """Crée une notification dans l'app ; si `email_subject` est fourni, envoie aussi un email avec le lien."""
    notification = Notification(user_id=user.id, kind=kind, message=message, link=link or link_to(item),
                                created_at=clock.now(db))
    db.add(notification)
    db.flush()
    if email_subject:
        body = f"Bonjour {user.name},\n\n{message}\n\nOuvrir : {config.APP_BASE_URL}/notifications/{notification.id}\n"
        send_email(db, user.email, email_subject, body)
    return notification


def send_email(db: Session, to: str, subject: str, body: str) -> None:
    sent = False
    if config.SMTP_HOST:
        try:
            msg = EmailMessage()
            msg["From"], msg["To"], msg["Subject"] = config.EMAIL_FROM, to, subject
            msg.set_content(body)
            with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=10) as smtp:
                smtp.starttls()
                if config.SMTP_USER:
                    smtp.login(config.SMTP_USER, config.SMTP_PASSWORD)
                smtp.send_message(msg)
            sent = True
        except (OSError, smtplib.SMTPException) as exc:
            log.warning("Échec d'envoi d'email à %s : %s", to, exc)
    db.add(EmailOutbox(to=to, subject=subject, body=body, sent=sent, created_at=clock.now(db)))
    db.flush()


def unread_count(db: Session, user: User) -> int:
    return db.scalar(select(func.count()).select_from(Notification)
                     .where(Notification.user_id == user.id, Notification.read.is_(False))) or 0
