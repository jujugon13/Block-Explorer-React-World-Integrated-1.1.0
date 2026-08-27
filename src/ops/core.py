"""Operations dashboard aggregation, destination policy, and push coalescing."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from threading import Event, Lock

from src.shared import (
    DashboardPublisher,
    OpsSnapshot,
    OpsSnapshotReader,
    OpsIndexingCommands,
    Principal,
    PublicError,
    Request,
)


DASHBOARD_DESTINATION = "/topic/dashboard"


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("operations instants must be timezone-aware")
    return moment


class DashboardService:
    """Calculate the complete dashboard body from one database snapshot."""

    def __init__(
        self,
        reader: OpsSnapshotReader,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        worker_dead_threshold: timedelta = timedelta(seconds=30),
        indexing_commands: OpsIndexingCommands | None = None,
    ) -> None:
        if worker_dead_threshold <= timedelta(0):
            raise ValueError("worker dead threshold must be positive")
        self._reader = reader
        self._clock = clock
        self._worker_dead_threshold = worker_dead_threshold
        self._indexing_commands = indexing_commands

    @staticmethod
    def _admin(request: Request) -> None:
        principal = request.principal
        if principal is None:
            raise PublicError("COMMON-007")
        if "ADMIN" not in principal.roles:
            raise PublicError("ROLE-002")

    @staticmethod
    def _data(result: object) -> dict[str, object]:
        data = getattr(result, "data", None)
        if not isinstance(data, dict):
            raise TypeError("indexing command returned an invalid result")
        return data

    def summary(self) -> dict[str, object]:
        now = _aware(self._clock())
        snapshot = self._reader.read_ops_snapshot(now)
        if not isinstance(snapshot, OpsSnapshot):
            raise TypeError("operations reader returned an invalid snapshot")

        documents = tuple(
            row
            for row in snapshot.documents
            if row.deleted_at is None and row.status != "DELETED"
        )
        completed = tuple(
            row
            for row in snapshot.jobs
            if row.status == "INDEXED"
            and row.first_started_at is not None
            and row.completed_at is not None
        )
        durations = tuple(
            (_aware(row.completed_at) - _aware(row.first_started_at)).total_seconds() * 1000.0
            for row in completed
        )
        dead_at = now - self._worker_dead_threshold
        active_workers = sum(
            row.status in {"ACTIVE", "IDLE"} and _aware(row.last_heartbeat) > dead_at
            for row in snapshot.workers
        )
        recent_at = now - timedelta(hours=24)
        recent_searches = sum(
            recent_at <= _aware(row.requested_at) <= now for row in snapshot.searches
        )

        return {
            "documents": {
                "total": len(documents),
                "searchable": sum(row.status == "INDEXED" for row in documents),
                "pendingIndex": sum(
                    row.status in {"UPLOADED", "INDEXING"} for row in documents
                ),
            },
            "jobs": {
                "pending": sum(row.status == "PENDING" for row in snapshot.jobs),
                "processing": sum(row.status == "PROCESSING" for row in snapshot.jobs),
                "failed": sum(row.status == "FAILED" for row in snapshot.jobs),
                "avgProcessMs": sum(durations) / len(durations) if durations else None,
            },
            "workers": {
                "activeCount": active_workers,
                "totalCount": len(snapshot.workers),
            },
            "search": {"recent24hCount": recent_searches},
        }

    def handler(self, request: Request) -> dict[str, object]:
        self._admin(request)
        return self.summary()

    def retry_job(
        self,
        request: Request,
        *,
        after_success: Callable[[], None] | None = None,
    ) -> dict[str, object]:
        self._admin(request)
        raw_job_id = request.path_params.get("jobId")
        try:
            job_id = int(raw_job_id) if raw_job_id is not None else 0
        except ValueError:
            raise PublicError("COMMON-002") from None
        if job_id < 1:
            raise PublicError("COMMON-002")
        if self._indexing_commands is None:
            raise RuntimeError("indexing commands are not configured")
        data = self._data(self._indexing_commands.manual_retry(job_id, self._clock()))
        public = {"jobId": data.get("jobId"), "status": data.get("status")}
        if (
            isinstance(public["jobId"], bool)
            or not isinstance(public["jobId"], int)
            or not isinstance(public["status"], str)
        ):
            raise TypeError("indexing command returned an invalid retry result")
        if after_success is not None:
            after_success()
        return public

    def retry_all(
        self,
        request: Request,
        *,
        after_success: Callable[[], None] | None = None,
    ) -> dict[str, object]:
        self._admin(request)
        if self._indexing_commands is None:
            raise RuntimeError("indexing commands are not configured")
        return self._data(
            self._indexing_commands.retry_all(self._clock(), on_success=after_success)
        )


class DashboardDestinationPolicy:
    """STOMP destination authorization without implementing the protocol itself."""

    @staticmethod
    def can_subscribe(destination: str, principal: Principal | None) -> bool:
        return (
            destination == DASHBOARD_DESTINATION
            and principal is not None
            and "ADMIN" in principal.roles
        )

    @staticmethod
    def can_send(destination: str, principal: Principal | None) -> bool:
        del destination, principal
        return False


class DashboardPush:
    """Coalesce committed changes and publish at most once per fixed-delay tick."""

    def __init__(
        self,
        service: DashboardService,
        publisher: DashboardPublisher,
        *,
        debounce_seconds: float = 0.3,
    ) -> None:
        if debounce_seconds <= 0:
            raise ValueError("dashboard debounce must be positive")
        self.service = service
        self.publisher = publisher
        self.debounce_seconds = debounce_seconds
        self._changed = False
        self._changed_lock = Lock()
        self._tick_lock = Lock()

    def state_transition_committed(self) -> None:
        """Transaction after-commit callback."""

        with self._changed_lock:
            self._changed = True

    def state_transition_rolled_back(self) -> None:
        """Transaction rollback callback; rolled-back state is never announced."""

    def tick(self) -> bool:
        if not self._tick_lock.acquire(blocking=False):
            return False
        try:
            with self._changed_lock:
                if not self._changed:
                    return False
                self._changed = False
            try:
                self.publisher.publish(DASHBOARD_DESTINATION, self.service.summary())
            except Exception:
                with self._changed_lock:
                    self._changed = True
                raise
            return True
        finally:
            self._tick_lock.release()

    def run(self, stop: Event) -> None:
        """Always-enabled fixed-delay loop; the application owns its thread."""

        while not stop.wait(self.debounce_seconds):
            self.tick()


def register_ops_routes(
    app: object,
    service: DashboardService,
    push: DashboardPush | None = None,
) -> None:
    """Attach all confirmed operations REST endpoints to the platform router."""

    app.add_route("GET", "/admin/dashboard/summary", service.handler)
    after_success = push.state_transition_committed if push is not None else None
    app.add_route(
        "POST",
        "/admin/embedding-jobs/{jobId}/retry",
        lambda request: service.retry_job(request, after_success=after_success),
    )
    app.add_route(
        "POST",
        "/admin/embedding-jobs/retry-all",
        lambda request: service.retry_all(request, after_success=after_success),
    )
