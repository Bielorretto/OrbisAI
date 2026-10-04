from datetime import timedelta

import pytest

from app import clock
from app.services import billing
from app.services.workflow import WorkflowError
from tests.conftest import dossier, task_by_title


@pytest.mark.parametrize("minutes, expected", [(1, 6), (6, 6), (7, 12), (61, 66), (0, 6)])
def test_rounding_to_tenth_of_hour(minutes, expected):
    assert billing.round_minutes(minutes) == expected


def test_timer_rounds_and_writes_label(db, people):
    task = task_by_title(db, "Notification du changement de contrôle")
    lea = people["Léa Garnier"]
    entry = billing.start_timer(db, lea, task)
    clock.advance(db, timedelta(minutes=50))
    billing.stop_timer(db, lea, entry.id, "relecture du dossier de notification")
    assert entry.minutes == 54 and not entry.running
    assert "notification" in entry.label.lower()


def test_only_worker_can_log_time(db, people):
    task = task_by_title(db, "Notification du changement de contrôle")
    with pytest.raises(WorkflowError):
        billing.add_entry(db, people["Emma Rolland"], task, 60, "", clock.now(db).date())


def test_intern_time_is_not_billable_by_default(db, people):
    task = task_by_title(db, "Due diligence réglementaire")
    entry = billing.add_entry(db, people["Hugo Lambert"], task, 90, "revue agréments", clock.now(db).date())
    assert entry.billable is False and entry.amount == 0


def test_budget_alerts_once_per_threshold(db, people):
    task = task_by_title(db, "Notification du changement de contrôle")  # 4 h estimées, 1,5 h saisies
    lea = people["Léa Garnier"]
    billing.add_entry(db, lea, task, 102, "", clock.now(db).date())
    assert task.budget_alert_level == 80
    billing.add_entry(db, lea, task, 12, "", clock.now(db).date())
    assert task.budget_alert_level == 80
    billing.add_entry(db, lea, task, 60, "", clock.now(db).date())
    assert task.budget_alert_level == 100


def test_pre_invoice_flow(db, people):
    fund = dossier(db, 27)  # Innovatech : 10 h de Victor Rey validées, le temps de la stagiaire n'est pas facturable
    invoice = billing.create_pre_invoice(db, people["Camille Dubois"], fund.id)
    assert invoice.total == pytest.approx(10 * 320)
    first = invoice.lines[0]
    billing.update_pre_invoice(db, invoice.id, {first.id: {"label": "Libellé revu", "write_off_pct": "50"}})
    assert first.label == "Libellé revu" and invoice.total == pytest.approx(10 * 320 - first.gross_amount / 2)
    billing.validate_pre_invoice(db, invoice.id)
    with pytest.raises(WorkflowError):
        billing.create_pre_invoice(db, people["Camille Dubois"], fund.id)
    assert "TOTAL" in billing.export_csv(invoice)


def test_suggestions_come_from_agenda(db, people):
    suggestions = billing.suggestions(db, people["Léa Garnier"], clock.now(db).date())
    assert any(s["minutes"] == 120 for s in suggestions)
