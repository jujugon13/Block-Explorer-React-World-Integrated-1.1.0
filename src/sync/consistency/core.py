"""Consistency controls whose concrete detection rules are injected."""

from __future__ import annotations

from src.shared import Identifier

from ..model import ConsistencyIssueRow, ReconciliationRunRow


ISSUE_TYPES = frozenset(
    {
        "MISSING_JOB",
        "MISSING_CHUNKS",
        "MISSING_EMBEDDINGS",
        "MODEL_MISMATCH",
        "INVALID_CURRENT_VERSION",
        "STALLED_VERSION",
        "DELETED_DOCUMENT_RESIDUE",
        "ORPHANED_DATA",
    }
)
SEVERITIES = frozenset({"WARNING", "ERROR", "CRITICAL"})


class ConsistencyControls:
    """Mixin for U-11 detector injection and confirmed issue transitions."""

    def add_issue(
        self,
        issue_type: str,
        severity: str,
        *,
        safe_to_repair: bool = False,
        issue_id: str | None = None,
        now=None,
    ) -> ConsistencyIssueRow:
        if issue_type not in ISSUE_TYPES or severity not in SEVERITIES:
            raise ValueError("invalid consistency finding")
        moment = self._now(now)
        with self.store.transaction():
            identifier = issue_id or self._id()
            existing = self.store.get_issue(identifier, for_update=True)
            if existing is not None:
                return existing
            issue = ConsistencyIssueRow(
                identifier,
                issue_type,
                severity,
                "OPEN",
                safe_to_repair,
                moment,
                moment,
            )
            self.store.insert_issue(issue)
            return issue

    def _resolve_finding(self, issue_id: str | None, moment) -> None:
        if issue_id is None:
            return
        with self.store.transaction():
            issue = self.store.get_issue(issue_id, for_update=True)
            if issue is None or issue.status in {"RESOLVED", "IGNORED"}:
                return
            issue.status = "RESOLVED"
            issue.updated_at = moment
            self.store.save_issue(issue)

    def issue(self, issue_id: str) -> ConsistencyIssueRow:
        issue = self.store.get_issue(issue_id)
        if issue is None:
            self._fail("SYNC-005")
        return issue

    def ignore_issue(
        self,
        issue_id: str,
        reason: str,
        *,
        actor_id: Identifier,
        now=None,
    ) -> ConsistencyIssueRow:
        if not reason.strip():
            self._fail("COMMON-002", "reason: 공백일 수 없습니다.")
        moment = self._now(now)
        with self.store.transaction():
            issue = self.store.get_issue(issue_id, for_update=True)
            if issue is None:
                self._fail("SYNC-005")
            if issue.status != "OPEN":
                self._fail("SYNC-007")
            issue.status = "IGNORED"
            issue.ignored_reason = reason
            issue.updated_at = moment
            self.store.save_issue(issue)
            self._action(
                "ISSUE_IGNORED",
                "CONSISTENCY_ISSUE",
                issue.id,
                actor_id,
                moment,
                reason,
            )
            return issue

    def repair_issue(
        self,
        issue_id: str,
        *,
        actor_id: Identifier,
        now=None,
    ) -> ConsistencyIssueRow:
        moment = self._now(now)
        with self.store.transaction():
            issue = self.store.get_issue(issue_id, for_update=True)
            if issue is None:
                self._fail("SYNC-005")
            if issue.status != "OPEN" or not issue.safe_to_repair:
                self._fail("SYNC-006")
            issue.status = "REPAIRING"
            issue.updated_at = moment
            self.store.save_issue(issue)
            self._action(
                "ISSUE_REPAIR_REQUESTED",
                "CONSISTENCY_ISSUE",
                issue.id,
                actor_id,
                moment,
            )
            return issue

    def reconcile(
        self,
        *,
        cursor: str | None = None,
        mode: str = "DRY_RUN",
        actor_id: Identifier,
        now=None,
    ) -> ReconciliationRunRow:
        if mode not in {"DRY_RUN", "REPAIR"}:
            self._fail("COMMON-002")
        moment = self._now(now)
        run = ReconciliationRunRow(
            self._id(), mode, cursor, "RUNNING", moment
        )
        with self.store.transaction():
            self.store.insert_reconciliation(run)
        try:
            findings = (
                ()
                if self._detector is None
                else self._detector.detect(
                    cursor=cursor,
                    mode=mode,
                    limit=self.reconciliation_batch,
                )
            )
            for finding in findings:
                if finding.resolved:
                    self._resolve_finding(finding.issue_id, moment)
                else:
                    self.add_issue(
                        finding.issue_type,
                        finding.severity,
                        safe_to_repair=finding.safe_to_repair,
                        issue_id=finding.issue_id,
                        now=moment,
                    )
            with self.store.transaction():
                run.status = "COMPLETED"
                run.completed_at = moment
                self.store.save_reconciliation(run)
                self._action(
                    "RECONCILIATION_REQUESTED",
                    "RECONCILIATION",
                    run.id,
                    actor_id,
                    moment,
                )
            return run
        except Exception:
            with self.store.transaction():
                run.status = "FAILED"
                run.completed_at = moment
                self.store.save_reconciliation(run)
            raise
