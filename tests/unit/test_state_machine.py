from app.domain.state_machine import is_legal_transition
from app.models.enums import TicketStatus


def test_legal_transitions() -> None:
    assert is_legal_transition(TicketStatus.OPEN, TicketStatus.IN_PROGRESS) is True
    assert is_legal_transition(TicketStatus.IN_PROGRESS, TicketStatus.RESOLVED) is True
    assert is_legal_transition(TicketStatus.RESOLVED, TicketStatus.CLOSED) is True


def test_illegal_transitions() -> None:
    all_statuses = list(TicketStatus)
    # P16 grew the table from three rows to six (spec05 §4). The exhaustive
    # matrix assertion lives in test_lifecycle_v2.py.
    legal_pairs = {
        (TicketStatus.OPEN, TicketStatus.IN_PROGRESS),
        (TicketStatus.IN_PROGRESS, TicketStatus.RESOLVED),
        (TicketStatus.RESOLVED, TicketStatus.CLOSED),
        (TicketStatus.IN_PROGRESS, TicketStatus.PENDING_CUSTOMER),
        (TicketStatus.PENDING_CUSTOMER, TicketStatus.IN_PROGRESS),
        (TicketStatus.RESOLVED, TicketStatus.IN_PROGRESS),
    }

    for from_status in all_statuses:
        for to_status in all_statuses:
            if (from_status, to_status) not in legal_pairs:
                assert is_legal_transition(from_status, to_status) is False
