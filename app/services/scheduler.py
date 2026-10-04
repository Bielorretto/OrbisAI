"""Planificateur : tourne en tâche de fond (toutes les SCHEDULER_INTERVAL_SECONDS) et à la demande en démo.

1. Notifie l'associé quand un dossier à attribuer arrive à J-x (un email récapitulatif par associé).
2. Fait expirer les propositions restées sans réponse et passe au collaborateur suivant.
3. Rappelle l'échéance au responsable quelques jours avant (dossiers en cours et tâches).
"""
import asyncio
import logging
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock, config
from app.db import SessionLocal
from app.models import Matter, MatterStatus, Task, TaskStatus, User
from app.services.notifications import log_event, notify, send_email
from app.services.workflow import expire_proposals

log = logging.getLogger("loickaton.scheduler")


def tick(db: Session) -> dict:
    now = clock.now(db)
    summary = {"to_assign": 0, "expired": 0, "reminders": 0}

    # 1. Notification J-x à l'associé : dossiers à attribuer
    by_partner: dict[int, list[Matter]] = defaultdict(list)
    for matter in db.scalars(select(Matter).where(Matter.status == MatterStatus.TO_ASSIGN,
                                                  Matter.notified_at.is_(None))):
        if matter.deadline - timedelta(days=matter.notify_days_before) <= now:
            matter.notified_at = now
            days = max(0, (matter.deadline - now).days)
            for partner in matter.partners or [matter.created_by]:
                notify(db, partner, "to_assign",
                       f"Le dossier « {matter.name} » est à J-{days} : choisissez à qui le confier.",
                       matter, link=f"/matters/{matter.id}/assign")
                by_partner[partner.id].append(matter)
            log_event(db, matter, "partner_notified", f"Associés notifiés (J-{days})")
    for partner_id, matters in by_partner.items():
        partner = db.get(User, partner_id)
        lines = "\n".join(f"- {m.name} (échéance {m.deadline:%d/%m}) : {config.APP_BASE_URL}/matters/{m.id}/assign"
                          for m in matters)
        send_email(db, partner.email, f"{len(matters)} dossier(s) à attribuer",
                   f"Bonjour {partner.name},\n\nCes dossiers approchent de leur échéance :\n{lines}\n")
    summary["to_assign"] = len({m.id for ms in by_partner.values() for m in ms})

    # 2. Propositions expirées -> collaborateur suivant
    summary["expired"] = expire_proposals(db, now)

    # 3. Rappels d'échéance : dossiers en cours (au responsable) et tâches (au responsable et au stagiaire)
    reminder_limit = now + timedelta(days=config.DEADLINE_REMINDER_DAYS)
    for matter in db.scalars(select(Matter).where(Matter.status == MatterStatus.ACTIVE,
                                                  Matter.reminder_sent_at.is_(None), Matter.deadline <= reminder_limit)):
        matter.reminder_sent_at = now
        for lawyer in matter.lawyers:
            notify(db, lawyer, "deadline_reminder",
                   f"Rappel : échéance du dossier « {matter.name} » le {matter.deadline:%d/%m à %Hh%M}.", matter,
                   email_subject=f"Échéance proche : {matter.name}")
        summary["reminders"] += 1
    for task in db.scalars(select(Task).where(Task.status.in_(TaskStatus.ACTIVE_WORK),
                                              Task.reminder_sent_at.is_(None), Task.deadline <= reminder_limit)):
        task.reminder_sent_at = now
        recipients = {u.id: u for u in (task.assignee, task.delegate) if u}
        for user in recipients.values():
            notify(db, user, "deadline_reminder",
                   f"Rappel : « {task.title} » est due le {task.deadline:%d/%m à %Hh%M}.", task,
                   email_subject=f"Deadline proche : {task.title}")
        summary["reminders"] += 1

    db.flush()
    return summary


def run_once() -> dict:
    with SessionLocal() as db:
        summary = tick(db)
        db.commit()
        return summary


async def loop() -> None:
    while True:
        await asyncio.sleep(config.SCHEDULER_INTERVAL_SECONDS)
        try:
            summary = await asyncio.to_thread(run_once)
            if any(summary.values()):
                log.info("Planificateur : %s", summary)
        except Exception:  # noqa: BLE001 - le planificateur ne doit jamais s'arrêter
            log.exception("Erreur du planificateur")
