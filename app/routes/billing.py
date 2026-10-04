"""Feuille de temps, chronomètre, pré-facturation."""
from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.db import get_db
from app.models import PreInvoice, Role, Task, TaskStatus, User
from app.routes.common import current_user, redirect, render, require_role
from app.services import billing
from app.services.workflow import WorkflowError

router = APIRouter()


def _safe_next(url: str) -> str:
    return url if url.startswith("/") else "/timesheet"


def _try(db: Session, action, next_url: str, success: str):
    try:
        action()
        db.commit()
        return redirect(next_url, success)
    except WorkflowError as exc:
        db.rollback()
        return redirect(next_url, str(exc), error=True)


# --------------------------------------------------------------------------- feuille de temps

@router.get("/timesheet")
def timesheet(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    today = clock.now(db).date()
    loggable = [t for t in db.scalars(select(Task).where(Task.status.in_(billing.LOGGABLE_STATUSES))
                                      .order_by(Task.deadline)) if billing.can_log_time(t, user)]
    entries = billing.entries_for(db, user)
    return render(request, "timesheet.html", db, user, today=today, loggable=loggable, entries=entries,
                  suggestions=billing.suggestions(db, user, today),
                  today_minutes=billing.total_logged_minutes(db, user, today),
                  drafts=sum(1 for e in entries if e.status == "draft" and not e.running))


@router.post("/timer/start")
def timer_start(task_id: int = Form(...), next: str = Form("/timesheet"), db: Session = Depends(get_db),
                user: User = Depends(current_user)):
    task = db.get(Task, task_id)
    return _try(db, lambda: billing.start_timer(db, user, task), _safe_next(next), "Chronomètre démarré.")


@router.post("/timer/stop")
def timer_stop(entry_id: int = Form(...), note: str = Form(""), next: str = Form("/timesheet"),
               db: Session = Depends(get_db), user: User = Depends(current_user)):
    return _try(db, lambda: billing.stop_timer(db, user, entry_id, note), _safe_next(next),
                "Temps enregistré, libellé rédigé par l'IA (modifiable).")


@router.post("/time-entries")
def add_entry(task_id: int = Form(...), hours: float = Form(0), minutes: float = Form(0), note: str = Form(""),
              work_date: str = Form(""), next: str = Form("/timesheet"), db: Session = Depends(get_db),
              user: User = Depends(current_user)):
    task = db.get(Task, task_id)
    day = date.fromisoformat(work_date) if work_date else clock.now(db).date()
    return _try(db, lambda: billing.add_entry(db, user, task, hours * 60 + minutes, note, day), _safe_next(next),
                "Temps ajouté, libellé rédigé par l'IA (modifiable).")


@router.post("/time-entries/{entry_id}")
def update_entry(entry_id: int, label: str = Form(...), hours: float = Form(...), db: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    return _try(db, lambda: billing.update_entry(db, user, entry_id, label=label, minutes=hours * 60), "/timesheet",
                "Saisie mise à jour.")


@router.post("/time-entries/{entry_id}/relabel")
def relabel(entry_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return _try(db, lambda: billing.regenerate_label(db, user, entry_id), "/timesheet", "Libellé régénéré.")


@router.post("/time-entries/{entry_id}/delete")
def delete_entry(entry_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return _try(db, lambda: billing.delete_entry(db, user, entry_id), "/timesheet", "Saisie supprimée.")


@router.post("/time-entries/validate")
def validate(db: Session = Depends(get_db), user: User = Depends(current_user)):
    count = billing.validate_entries(db, user)
    db.commit()
    return redirect("/timesheet", f"{count} saisie(s) validée(s).")


# --------------------------------------------------------------------------- facturation (associé)

@router.get("/billing")
def billing_page(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, *Role.ASSIGNERS)
    tasks = db.scalars(select(Task).where(Task.status.in_(TaskStatus.ACTIVE_WORK + (TaskStatus.DONE,)))).all()
    budgets = sorted(((t, billing.task_budget(db, t)) for t in tasks), key=lambda x: -x[1]["pct"])
    return render(request, "billing.html", db, user, overview=billing.billing_overview(db),
                  invoices=billing.pre_invoices(db), budgets=budgets)


@router.post("/pre-invoices")
def create_pre_invoice(matter_id: int = Form(...), db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        invoice = billing.create_pre_invoice(db, user, matter_id)
        db.commit()
    except WorkflowError as exc:
        db.rollback()
        return redirect("/billing", str(exc), error=True)
    return redirect(f"/pre-invoices/{invoice.id}", "Pré-facture générée : relisez et ajustez avant de valider.")


@router.get("/pre-invoices/{invoice_id}")
def pre_invoice(invoice_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, *Role.ASSIGNERS)
    invoice = db.get(PreInvoice, invoice_id)
    if not invoice:
        return redirect("/billing", "Pré-facture introuvable", error=True)
    return render(request, "pre_invoice.html", db, user, invoice=invoice)


@router.post("/pre-invoices/{invoice_id}")
async def update_pre_invoice(invoice_id: int, request: Request, db: Session = Depends(get_db),
                             user: User = Depends(current_user)):
    require_role(user, *Role.ASSIGNERS)
    form = await request.form()
    lines: dict[int, dict] = {}
    for key, value in form.items():
        field, _, line_id = key.rpartition("_")
        if field in ("label", "write_off_pct") and line_id.isdigit():
            lines.setdefault(int(line_id), {})[field] = value
    action = form.get("action", "save")
    try:
        billing.update_pre_invoice(db, invoice_id, lines)
        if action == "validate":
            billing.validate_pre_invoice(db, invoice_id)
        db.commit()
    except (WorkflowError, ValueError) as exc:
        db.rollback()
        return redirect(f"/pre-invoices/{invoice_id}", str(exc), error=True)
    message = "Pré-facture validée : les temps sont marqués comme facturés." if action == "validate" else "Enregistré."
    return redirect(f"/pre-invoices/{invoice_id}", message)


@router.post("/pre-invoices/{invoice_id}/delete")
def delete_pre_invoice(invoice_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, *Role.ASSIGNERS)
    return _try(db, lambda: billing.delete_pre_invoice(db, invoice_id), "/billing",
                "Brouillon supprimé : les temps sont de nouveau disponibles.")


@router.get("/pre-invoices/{invoice_id}/export.csv")
def export_csv(invoice_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    require_role(user, *Role.ASSIGNERS)
    invoice = db.get(PreInvoice, invoice_id)
    if not invoice:
        return redirect("/billing", "Pré-facture introuvable", error=True)
    filename = f"pre-facture-{invoice.matter.reference}-{invoice.id}.csv"
    # BOM UTF-8 pour qu'Excel affiche correctement les accents
    return Response("﻿" + billing.export_csv(invoice), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
