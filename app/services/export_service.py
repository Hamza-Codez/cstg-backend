"""CSV export of the ticket list (spec09 §6).

Read-only, so no unit of work — the same shape as `MetricsService`.

The generator is the point: rows are formatted and yielded one at a time from a
server-side cursor, so peak memory is a chunk rather than the whole result set.
This process also runs the SLA monitor, and a 100k-row export that buffered
everything would stall breach detection.
"""

import csv
import io
from collections.abc import AsyncIterator

from app.config import get_settings
from app.core.authorization import Principal
from app.domain.csv_safety import escape_field
from app.domain.errors import BusinessRuleViolation
from app.repositories.ticket_repo import TicketRepository
from app.repositories.ticket_scope import scope_for
from app.schemas.ticket_filters import TicketFilters, authorize_filters

#: Deliberately no body and no comment text — operational data, not a content
#: dump (spec09 §6).
COLUMNS = (
    "ticket_id",
    "customer_name",
    "tier",
    "subject",
    "category",
    "priority",
    "status",
    "assignee_name",
    "created_at",
    "deadline",
    "sla_due_at",
    "sla_paused_seconds",
    "resolved_at",
    "sla_breached_at",
    "escalation_level",
    "reopen_count",
)


class ExportService:
    def __init__(self, repo: TicketRepository) -> None:
        self.repo = repo

    async def stream_tickets_csv(
        self, principal: Principal, filters: TicketFilters
    ) -> AsyncIterator[str]:
        """Yield the CSV a chunk of rows at a time.

        The cap is checked with a COUNT *before* the first byte is yielded.
        Once a `StreamingResponse` has begun there is no way to turn a partial
        file into a 422, and a silently truncated export is worse than a
        refused one.
        """
        authorize_filters(principal, filters)
        scope = scope_for(principal).predicate()

        max_rows = get_settings().export_max_rows
        total = await self.repo.count_matching(scope=scope, filters=filters)
        if total > max_rows:
            raise BusinessRuleViolation(
                f"That export is {total:,} rows, over the {max_rows:,} limit. "
                "Narrow the date range or add a filter."
            )

        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")

        def drain() -> str:
            text = buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
            return text

        writer.writerow(COLUMNS)
        yield drain()

        rows = 0
        async for row in self.repo.stream_for_export(scope=scope, filters=filters):
            writer.writerow([escape_field(value) for value in row])
            rows += 1
            # Flush per chunk rather than per row: one `yield` per row makes the
            # response a few hundred thousand tiny writes.
            if rows % 500 == 0:
                yield drain()

        remainder = drain()
        if remainder:
            yield remainder
