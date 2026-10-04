from datetime import timedelta

import pytest

from app import clock
from app.models import TimeEntryStatus
from app.services import billing
from app.services.workflow import WorkflowError
from tests.conftest import matter, task_by_title


@pytest.mark.parametrize("minutes, expected", [(1, 6), (6, 6), (7, 12), (61, 66), (0, 6)])
def test_rounding_to_tenth_of_hour(minutes, expected):
    assert billing.round_minutes(minutes) == expected


def test_timer_rounds_and_writes_label(db, people):
    task = task_by_title(db, "Rédiger les conclusions en défense")
    julien = people["Julien Moreau"]
    entry = billing.start_timer(db, julien, task)
    clock.advance(db, timedelta(minutes=50))
    billing.stop_timer(db, julien, entry.id, "relecture des attestations")
    assert entry.minutes == 54 and not entry.running
    assert "attestations" in entry.label


def test_only_worker_can_log_time(db, people):
    task = task_by_title(db, "Rédiger les conclusions en défense")
    with pytest.raises(WorkflowError):
        billing.add_entry(db, people["Camille Bernard"], task, 60, "", clock.now(db).date())


def test_intern_time_is_not_billable_by_default(db, people):
    task = task_by_title(db, "Mise à jour des statuts")
    entry = billing.add_entry(db, people["Chloé Martin"], task, 90, "statuts", clock.now(db).date())
    assert entry.billable is False and entry.amount == 0


def test_budget_alerts_once_per_threshold(db, people):
    task = task_by_title(db, "Rédiger les conclusions en défense")  # 10 h estimées, 7 h déjà saisies
    julien = people["Julien Moreau"]
    billing.add_entry(db, julien, task, 60, "", clock.now(db).date())
    assert task.budget_alert_level == 80
    billing.add_entry(db, julien, task, 12, "", clock.now(db).date())
    assert task.budget_alert_level == 80  # pas de nouvelle alerte sous 100 %
    billing.add_entry(db, julien, task, 120, "", clock.now(db).date())
    assert task.budget_alert_level == 100


def test_pre_invoice_flow(db, people):
    corporate = matter(db, "2026-021")
    invoice = billing.create_pre_invoice(db, people["Hélène Marchal"], corporate.id)
    # 4 saisies validées de Camille ; celle de la stagiaire (non facturable) est exclue
    assert len(invoice.lines) == 4
    assert invoice.total == pytest.approx(9 * 320)

    first = invoice.lines[0]
    billing.update_pre_invoice(db, invoice.id, {first.id: {"label": "Libellé revu", "write_off_pct": "50"}})
    assert first.label == "Libellé revu"
    assert invoice.total == pytest.approx(9 * 320 - first.gross_amount / 2)

    billing.validate_pre_invoice(db, invoice.id)
    assert all(db.get(billing.TimeEntry, l.time_entry_id).status == TimeEntryStatus.INVOICED for l in invoice.lines)
    with pytest.raises(WorkflowError):
        billing.create_pre_invoice(db, people["Hélène Marchal"], corporate.id)
    assert "TOTAL" in billing.export_csv(invoice)


def test_entries_in_draft_invoice_are_not_invoiced_twice(db, people):
    corporate = matter(db, "2026-021")
    billing.create_pre_invoice(db, people["Hélène Marchal"], corporate.id)
    with pytest.raises(WorkflowError):
        billing.create_pre_invoice(db, people["Hélène Marchal"], corporate.id)


def test_suggestions_come_from_agenda(db, people):
    suggestions = billing.suggestions(db, people["Julien Moreau"], clock.now(db).date())
    assert suggestions and suggestions[0]["minutes"] == 150
