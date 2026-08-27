from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from tools.run_stage16_external import child_environment, preflight, read_dotenv
from tools.stage16_e2e import SCENARIOS, _recover_outage, run_parent
from tools.stage16_outage import RdsTarget, interrupt_and_observe


class Stage16ExternalRunnerTests(unittest.TestCase):
    def test_dotenv_parser_and_child_environment_do_not_mutate_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "DB_HOST='private-host'\nignored bare line\n"
                "DB_PASSWORD=private-password # comment\n",
                encoding="utf-8",
            )
            parsed = read_dotenv(path)
        parent = {"DB_HOST": "parent-host", "PYTHONPATH": "existing"}
        child = child_environment(parent, parsed)

        self.assertEqual("parent-host", child["DB_HOST"])
        self.assertEqual("private-password", child["DB_PASSWORD"])
        self.assertEqual({"DB_HOST": "parent-host", "PYTHONPATH": "existing"}, parent)
        self.assertTrue(child["PYTHONPATH"].endswith(os.pathsep + "existing"))

    def test_preflight_prints_presence_only_and_rejects_every_non_test_value(self):
        base = {
            **{name: f"private-{name}" for name in (
                "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD", "S3_BUCKET"
            )},
            "AWS_ACCESS_KEY_ID": "private-access",
            "AWS_SECRET_ACCESS_KEY": "private-secret",
            "AWS_REGION": "private-region",
        }
        for value in ("prod", "production", "development", "qa", ""):
            with self.subTest(value=value):
                output = io.StringIO()
                with redirect_stdout(output):
                    accepted = preflight({**base, "STAGE16_TEST_ENVIRONMENT": value})
                self.assertFalse(accepted)
                self.assertNotIn("private-", output.getvalue())

        output = io.StringIO()
        with redirect_stdout(output):
            accepted = preflight({**base, "STAGE16_TEST_ENVIRONMENT": "staging"})
        self.assertTrue(accepted)
        self.assertNotIn("private-", output.getvalue())

    def test_e2e_lists_every_scenario_and_direct_execution_rejects_qa(self):
        self.assertEqual(
            ("16-1", "16-2", "16-3", "16-4a", "16-4b", "16-5", "16-6"),
            SCENARIOS,
        )
        environment = {
            name: f"private-{name}"
            for name in (
                "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD", "S3_BUCKET"
            )
        }
        environment["STAGE16_TEST_ENVIRONMENT"] = "qa"
        output = io.StringIO()
        with patch.dict(os.environ, environment, clear=True), redirect_stdout(output):
            result = run_parent("16-1")
        self.assertEqual(2, result)
        self.assertIn('"status": "BLOCKED"', output.getvalue())
        self.assertNotIn("private-", output.getvalue())

    def test_multi_az_waits_for_delayed_topology_metadata_after_api_recovery(self):
        target = RdsTarget(
            "db-instance", "test-db", "arn", "endpoint", "writer", "az-a", "available"
        )
        changed = RdsTarget(
            "db-instance", "test-db", "arn", "endpoint", "writer", "az-b", "available"
        )
        with (
            patch("tools.stage16_outage._endpoint_matches", return_value=True),
            patch(
                "tools.stage16_outage.probe",
                side_effect=((True, True, True), (False, False, False), (True, True, True)),
            ) as probe,
            patch(
                "tools.stage16_outage.refresh",
                side_effect=(target, target, changed),
            ),
            patch("tools.stage16_outage.time.monotonic", side_effect=range(20)),
            patch("tools.stage16_outage.time.sleep"),
        ):
            result = interrupt_and_observe(
                object(), target, Mock(), "run", require_topology=True
            )

        self.assertEqual(3, probe.call_count)
        self.assertTrue(result["primaryAzChanged"])

    def test_outage_recovery_does_not_schedule_outbox_replay_in_the_future(self):
        now = datetime(2026, 8, 30, tzinfo=UTC)
        domain = Mock()
        domain.indexing.recover_expired.return_value = {"recovered": 1}
        domain.sync.recover_expired.return_value = [object()]
        domain.sync_dispatcher.tick.return_value = object()
        rows = (("INDEXED", "PROCESSED", 1, 1, 1, 1, 1),)
        with (
            patch("tools.stage16_e2e.datetime") as clock,
            patch("tools.stage16_e2e._finish_job"),
            patch("tools.stage16_e2e.observer", return_value=rows),
            patch("tools.stage16_e2e.Event") as event,
        ):
            clock.now.return_value = now
            recovered = _recover_outage(
                domain, 1, {"claimToken": "old"}, "event", "run"
            )

        self.assertTrue(recovered)
        domain.sync.recover_expired.assert_called_once_with(now=now)
        event.return_value.wait.assert_called_once_with(1.1)


if __name__ == "__main__":
    unittest.main()
