import uuid

import pytest

from app.core.authorization import Principal, authorize_comment_authoring
from app.domain.errors import Forbidden, NotFound
from app.models.enums import ActorType, CommentType, Role


class MockTicket:
    def __init__(self, customer_id: uuid.UUID | None = None, assignee_id: uuid.UUID | None = None):
        self.customer_id = customer_id
        self.assignee_id = assignee_id


def test_authorize_comment_authoring():
    admin = Principal(id=uuid.uuid4(), type=ActorType.USER, role=Role.ADMIN, is_active=True)
    dispatcher = Principal(
        id=uuid.uuid4(), type=ActorType.USER, role=Role.DISPATCHER, is_active=True
    )

    agent = Principal(id=uuid.uuid4(), type=ActorType.USER, role=Role.AGENT, is_active=True)
    customer = Principal(
        id=uuid.uuid4(), type=ActorType.CUSTOMER, role=Role.CUSTOMER, is_active=True
    )

    ticket_customer = MockTicket(customer_id=customer.id)
    ticket_agent = MockTicket(assignee_id=agent.id)
    ticket_other = MockTicket(customer_id=uuid.uuid4(), assignee_id=uuid.uuid4())

    # Admin and dispatcher can post any comment type anywhere
    for p in (admin, dispatcher):
        for t in (ticket_customer, ticket_agent, ticket_other):
            for c_type in CommentType:
                authorize_comment_authoring(p, t, c_type)  # Should not raise

    # Agent can only post on assigned tickets
    for c_type in CommentType:
        authorize_comment_authoring(agent, ticket_agent, c_type)  # Should not raise

        with pytest.raises(NotFound):
            authorize_comment_authoring(agent, ticket_other, c_type)

    # Customer can only post PUBLIC_REPLY on their own ticket
    authorize_comment_authoring(
        customer, ticket_customer, CommentType.PUBLIC_REPLY
    )  # Should not raise

    with pytest.raises(Forbidden):
        authorize_comment_authoring(customer, ticket_customer, CommentType.INTERNAL_NOTE)

    with pytest.raises(NotFound):
        authorize_comment_authoring(customer, ticket_other, CommentType.PUBLIC_REPLY)
