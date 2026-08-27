"""Administrator-facing sync state queries and controls."""

from __future__ import annotations

from src.shared import Identifier

from ..model import OperatorActionRow


class ManagementControls:
    """Mixin for sync administration without changing the service API."""

    def event(self, event_id: str):
        event = self.store.get_event(event_id)
        if event is None:
            self._fail("SYNC-001")
        return event

    def events(self, *, status: str | None = None, event_type: str | None = None):
        return tuple(
            sorted(
                (
                    event
                    for event in self.store.list_events()
                    if (status is None or event.status == status)
                    and (event_type is None or event.event_type == event_type)
                ),
                key=lambda event: (event.occurred_at, event.id),
            )
        )

    def retry_failed(self, event_id: str, *, actor_id: Identifier, now=None):
        moment = self._now(now)
        with self.store.transaction():
            event = self.store.get_event(event_id, for_update=True)
            if event is None:
                self._fail("SYNC-001")
            if event.status != "FAILED":
                self._fail("SYNC-004")
            event.status = "PENDING"
            event.max_retries += 1
            event.available_at = moment
            event.processed_at = None
            event.failed_at = None
            event.error_type = None
            event.error_message = None
            self._clear_owner(event)
            self.store.save_event(event)
            self._action("EVENT_RETRIED", "SYNC_EVENT", event.id, actor_id, moment)
            return event

    def attempts(self, event_id: str):
        self.event(event_id)
        return self.store.list_attempts(event_id)

    def issues(
        self,
        *,
        status: str | None = None,
        issue_type: str | None = None,
        severity: str | None = None,
    ):
        return tuple(
            sorted(
                (
                    issue
                    for issue in self.store.list_issues()
                    if (status is None or issue.status == status)
                    and (issue_type is None or issue.issue_type == issue_type)
                    and (severity is None or issue.severity == severity)
                ),
                key=lambda issue: (issue.created_at, issue.id),
            )
        )

    def summary(self) -> dict[str, int]:
        events = self.store.list_events()
        issues = self.store.list_issues()
        counts = {
            status: sum(event.status == status for event in events)
            for status in ("PENDING", "PROCESSING", "PROCESSED", "FAILED")
        }
        return {
            "pending": counts["PENDING"],
            "processing": counts["PROCESSING"],
            "processed": counts["PROCESSED"],
            "failed": counts["FAILED"],
            "openIssues": sum(issue.status == "OPEN" for issue in issues),
        }

    def _action(
        self,
        action_type: str,
        target_type: str,
        target_id: str,
        actor_id: Identifier,
        moment,
        reason: str | None = None,
    ) -> None:
        self.store.insert_action(
            OperatorActionRow(
                self._id(), action_type, target_type, target_id, actor_id, moment, reason
            )
        )
