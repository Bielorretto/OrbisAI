"""Suivi du temps, budget et pré-facturation.

Chaque saisie de temps est rattachée à une tâche, donc à un dossier et à un client : c'est
ce qui permet de pré-remplir la feuille de temps et de générer la pré-facture.
Le prototype gère uniquement les honoraires au temps passé.
"""
import csv
import io
import math
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock, config
from app.models import (CalendarEvent, Matter, PreInvoice, PreInvoiceLine, Role, Task, TaskStatus, TimeEntry,
                        TimeEntryStatus, User)
from app.services import ai
from app.services.notifications import log_event, notify
from app.services.workflow import _require

LOGGABLE_STATUSES = (TaskStatus.IN_PROGRESS, TaskStatus.IN_REVIEW, TaskStatus.DONE)


def round_minutes(minutes: float) -> int:
    """Arrondi au multiple supérieur (6 min = 1/10e d'heure), minimum une unité."""
    step = config.TIME_ROUNDING_MINUTES
    return max(step, int(math.ceil(minutes / step) * step))


def can_log_time(task: Task, user: User) -> bool:
    return task.status in LOGGABLE_STATUSES and user.id in (task.assignee_id, task.delegate_id)


def _new_entry(db: Session, user: User, task: Task, **fields) -> TimeEntry:
    billable = config.INTERN_TIME_BILLABLE if user.role == Role.INTERN else True
    entry = TimeEntry(user_id=user.id, task_id=task.id, billable=billable, rate=user.hourly_rate,
                      status=TimeEntryStatus.DRAFT, created_at=clock.now(db), **fields)
    db.add(entry)
    db.flush()
    return entry


# --------------------------------------------------------------------------- chronomètre et saisie

def running_timer(db: Session, user: User) -> TimeEntry | None:
    return db.scalar(select(TimeEntry).where(TimeEntry.user_id == user.id, TimeEntry.running.is_(True)))


def start_timer(db: Session, user: User, task: Task) -> TimeEntry:
    _require(can_log_time(task, user), "Vous ne pouvez pas saisir de temps sur cette tâche.")
    current = running_timer(db, user)
    if current:
        stop_timer(db, user, current.id, "")
    now = clock.now(db)
    return _new_entry(db, user, task, work_date=now.date(), started_at=now, running=True)


def stop_timer(db: Session, user: User, entry_id: int, note: str) -> TimeEntry:
    entry = db.get(TimeEntry, entry_id)
    _require(entry is not None and entry.user_id == user.id and entry.running, "Aucun chronomètre en cours.")
    elapsed = (clock.now(db) - entry.started_at).total_seconds() / 60
    entry.minutes = round_minutes(elapsed)
    entry.running = False
    entry.note = note.strip()
    entry.label = ai.billing_label(entry.task.title, entry.task.description, entry.note)
    db.flush()
    check_budget(db, entry.task)
    return entry


def add_entry(db: Session, user: User, task: Task, minutes: float, note: str, work_date: date) -> TimeEntry:
    _require(can_log_time(task, user), "Vous ne pouvez pas saisir de temps sur cette tâche.")
    _require(0 < minutes <= 24 * 60, "Durée invalide.")
    label = ai.billing_label(task.title, task.description, note)
    entry = _new_entry(db, user, task, work_date=work_date, minutes=round_minutes(minutes),
                       note=note.strip(), label=label)
    check_budget(db, task)
    return entry


def update_entry(db: Session, user: User, entry_id: int, *, label: str, minutes: float) -> TimeEntry:
    entry = db.get(TimeEntry, entry_id)
    _require(entry is not None and entry.user_id == user.id, "Saisie introuvable.")
    _require(entry.status == TimeEntryStatus.DRAFT and not entry.running, "Seul un brouillon peut être modifié.")
    _require(0 < minutes <= 24 * 60, "Durée invalide.")
    entry.label = label.strip()
    entry.minutes = round_minutes(minutes)
    db.flush()
    check_budget(db, entry.task)
    return entry


def regenerate_label(db: Session, user: User, entry_id: int) -> TimeEntry:
    entry = db.get(TimeEntry, entry_id)
    _require(entry is not None and entry.user_id == user.id and entry.status == TimeEntryStatus.DRAFT,
             "Saisie introuvable.")
    entry.label = ai.billing_label(entry.task.title, entry.task.description, entry.note)
    db.flush()
    return entry


def delete_entry(db: Session, user: User, entry_id: int) -> None:
    entry = db.get(TimeEntry, entry_id)
    _require(entry is not None and entry.user_id == user.id and entry.status == TimeEntryStatus.DRAFT,
             "Seul un brouillon peut être supprimé.")
    task = entry.task
    db.delete(entry)
    db.flush()
    check_budget(db, task)


def validate_entries(db: Session, user: User) -> int:
    entries = db.scalars(select(TimeEntry).where(TimeEntry.user_id == user.id, TimeEntry.running.is_(False),
                                                 TimeEntry.status == TimeEntryStatus.DRAFT)).all()
    for entry in entries:
        entry.status = TimeEntryStatus.VALIDATED
    db.flush()
    return len(entries)


def entries_for(db: Session, user: User, limit: int = 100) -> list[TimeEntry]:
    return list(db.scalars(select(TimeEntry).where(TimeEntry.user_id == user.id)
                           .order_by(TimeEntry.work_date.desc(), TimeEntry.id.desc()).limit(limit)))


def suggestions(db: Session, user: User, day: date) -> list[dict]:
    """Feuille de temps pré-remplie : tâches en cours de la personne, durée tirée des blocs d'agenda du jour."""
    tasks = db.scalars(select(Task).where(Task.status.in_(LOGGABLE_STATUSES[:2]))).all()
    mine = [t for t in tasks if (t.delegate_id or t.assignee_id) == user.id]
    already = set(db.scalars(select(TimeEntry.task_id).where(TimeEntry.user_id == user.id,
                                                             TimeEntry.work_date == day)))
    start, end = datetime.combine(day, datetime.min.time()), datetime.combine(day, datetime.max.time())
    out = []
    for task in mine:
        if task.id in already:
            continue
        blocks = db.scalars(select(CalendarEvent).where(
            CalendarEvent.user_id == user.id, CalendarEvent.task_id == task.id,
            CalendarEvent.start >= start, CalendarEvent.start <= end)).all()
        minutes = sum((b.end - b.start).total_seconds() / 60 for b in blocks)
        out.append({"task": task, "minutes": int(minutes),
                    "source": "agenda" if minutes else "tâche en cours (durée à compléter)"})
    return out


# --------------------------------------------------------------------------- budget

def task_budget(db: Session, task: Task) -> dict:
    entries = db.scalars(select(TimeEntry).where(TimeEntry.task_id == task.id, TimeEntry.running.is_(False))).all()
    hours = sum(e.hours for e in entries)
    amount = sum(e.amount for e in entries)
    pct = round(100 * hours / task.estimated_hours) if task.estimated_hours else 0
    return {"hours": round(hours, 2), "amount": round(amount, 2), "estimated_hours": task.estimated_hours,
            "budget": task.budget_amount, "pct": pct}


def check_budget(db: Session, task: Task) -> None:
    """Alerte l'associé et le responsable quand un seuil de consommation est franchi (une fois par seuil)."""
    pct = task_budget(db, task)["pct"]
    crossed = [t for t in config.BUDGET_ALERT_THRESHOLDS if pct >= t and t > task.budget_alert_level]
    if not crossed:
        return
    threshold = max(crossed)
    task.budget_alert_level = threshold
    message = f"Budget de « {task.title} » consommé à {pct} % ({threshold} % atteint)."
    log_event(db, task, "budget_alert", message)
    # Le collaborateur responsable et l'associé responsable du dossier
    recipients = {u.id: u for u in (task.assignee, task.matter.created_by) if u}
    for user in recipients.values():
        notify(db, user, "budget_alert", message, task)
    db.flush()


# --------------------------------------------------------------------------- pré-facturation

def _invoiceable_query(matter_id: int):
    already = select(PreInvoiceLine.time_entry_id)
    return (select(TimeEntry).join(Task).where(
        Task.matter_id == matter_id, TimeEntry.status == TimeEntryStatus.VALIDATED,
        TimeEntry.billable.is_(True), TimeEntry.id.not_in(already))
        .order_by(TimeEntry.work_date, TimeEntry.id))


def billing_overview(db: Session) -> list[dict]:
    """Par dossier : temps à facturer, brouillons non validés, déjà facturé, réductions."""
    rows = []
    for matter in db.scalars(select(Matter).order_by(Matter.reference)):
        entries = db.scalars(select(TimeEntry).join(Task).where(Task.matter_id == matter.id,
                                                                TimeEntry.running.is_(False))).all()
        if not entries:
            continue
        ready = db.scalars(_invoiceable_query(matter.id)).all()
        invoices = db.scalars(select(PreInvoice).where(PreInvoice.matter_id == matter.id,
                                                       PreInvoice.status == "validated")).all()
        invoiced = sum(i.total for i in invoices)
        write_off = sum(i.total_before_write_off - i.total for i in invoices)
        rows.append({
            "matter": matter,
            "hours_total": round(sum(e.hours for e in entries), 1),
            "hours_non_billable": round(sum(e.hours for e in entries if not e.billable), 1),
            "draft_count": sum(1 for e in entries if e.status == TimeEntryStatus.DRAFT),
            "ready_count": len(ready),
            "ready_amount": round(sum(e.amount for e in ready), 2),
            "invoiced": round(invoiced, 2),
            "write_off": round(write_off, 2),
        })
    return rows


def create_pre_invoice(db: Session, partner: User, matter_id: int) -> PreInvoice:
    _require(partner.is_assigner, "Seul un associé peut préparer une pré-facture.")
    entries = db.scalars(_invoiceable_query(matter_id)).all()
    _require(bool(entries), "Aucun temps validé et facturable à facturer sur ce dossier.")
    invoice = PreInvoice(matter_id=matter_id, created_by_id=partner.id, created_at=clock.now(db))
    for e in entries:
        invoice.lines.append(PreInvoiceLine(time_entry_id=e.id, work_date=e.work_date, user_name=e.user.name,
                                            label=e.label or e.task.title, hours=e.hours, rate=e.rate))
    db.add(invoice)
    db.flush()
    return invoice


def _draft_invoice(db: Session, invoice_id: int) -> PreInvoice:
    invoice = db.get(PreInvoice, invoice_id)
    _require(invoice is not None, "Pré-facture introuvable.")
    _require(invoice.status == "draft", "Cette pré-facture est déjà validée.")
    return invoice


def update_pre_invoice(db: Session, invoice_id: int, lines: dict[int, dict]) -> PreInvoice:
    """lines : {line_id: {"label": str, "write_off_pct": float}}"""
    invoice = _draft_invoice(db, invoice_id)
    for line in invoice.lines:
        if line.id in lines:
            line.label = lines[line.id].get("label", line.label).strip() or line.label
            line.write_off_pct = min(100.0, max(0.0, float(lines[line.id].get("write_off_pct", line.write_off_pct))))
    db.flush()
    return invoice


def validate_pre_invoice(db: Session, invoice_id: int) -> PreInvoice:
    invoice = _draft_invoice(db, invoice_id)
    invoice.status = "validated"
    invoice.validated_at = clock.now(db)
    for line in invoice.lines:
        db.get(TimeEntry, line.time_entry_id).status = TimeEntryStatus.INVOICED
    db.flush()
    return invoice


def delete_pre_invoice(db: Session, invoice_id: int) -> None:
    db.delete(_draft_invoice(db, invoice_id))
    db.flush()


def export_csv(invoice: PreInvoice) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["Date", "Intervenant", "Libellé", "Heures", "Taux HT", "Montant brut HT", "Réduction %",
                     "Montant HT"])
    for line in invoice.lines:
        writer.writerow([line.work_date.strftime("%d/%m/%Y"), line.user_name, line.label,
                         f"{line.hours:.2f}".replace(".", ","), f"{line.rate:.2f}".replace(".", ","),
                         f"{line.gross_amount:.2f}".replace(".", ","), f"{line.write_off_pct:g}",
                         f"{line.amount:.2f}".replace(".", ",")])
    writer.writerow(["", "", "TOTAL", "", "", "", "", f"{invoice.total:.2f}".replace(".", ",")])
    return buffer.getvalue()


def pre_invoices(db: Session) -> list[PreInvoice]:
    return list(db.scalars(select(PreInvoice).order_by(PreInvoice.id.desc())))


def total_logged_minutes(db: Session, user: User, day: date) -> int:
    return db.scalar(select(func.coalesce(func.sum(TimeEntry.minutes), 0))
                     .where(TimeEntry.user_id == user.id, TimeEntry.work_date == day)) or 0
