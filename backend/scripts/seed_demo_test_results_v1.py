"""Seed repeatable demo execution runs for the local TestIQ database.

The seed is deliberately scoped to the project named ``Test Project 5`` and
uses a dedicated generation id so it can be removed or recreated safely.
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db import connection  # noqa: E402


GENERATION_ID = "demo_gen_test_project_5_v1"
DEMO_RUN_IDS = ("demo_run_tp5_v1_001", "demo_run_tp5_v1_002", "demo_run_tp5_v1_003")


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def json_text(value) -> str:
    return json.dumps(value, separators=(",", ":"))


def main() -> None:
    with connection() as conn:
        project = conn.execute("SELECT * FROM project WHERE project_name=%s ORDER BY created_at DESC LIMIT 1", ("Test Project 5",)).fetchone()
        if not project:
            raise SystemExit("Project 'Test Project 5' was not found.")
        package = conn.execute("SELECT * FROM source_package_manifest WHERE project_id=%s ORDER BY package_version ASC LIMIT 1", (project["project_id"],)).fetchone()
        if not package:
            raise SystemExit("No source package was found for Test Project 5.")
        analysis = conn.execute("SELECT * FROM requirement_analysis WHERE package_id=%s ORDER BY analysis_version DESC LIMIT 1", (package["package_id"],)).fetchone()
        if not analysis:
            raise SystemExit("No requirement analysis was found for Test Project 5.")

        conn.execute("DELETE FROM test_execution_result WHERE run_id IN (%s,%s,%s)", DEMO_RUN_IDS)
        conn.execute("DELETE FROM test_run WHERE run_id IN (%s,%s,%s)", DEMO_RUN_IDS)
        conn.execute("DELETE FROM test_case_generation WHERE generation_id=%s", (GENERATION_ID,))

        now = datetime.now(timezone.utc).replace(microsecond=0)
        cases = [
            {"test_case_id": "TC-001", "title": "Eligible member receives refill notification", "expected_results": ["Notification is recorded for the eligible member."], "requirement_id": "BR-001", "review_status": "approved", "execution_status": "not_run"},
            {"test_case_id": "TC-002", "title": "Ineligible member does not receive refill notification", "expected_results": ["No notification is recorded for the ineligible member."], "requirement_id": "BR-002", "review_status": "approved", "execution_status": "not_run"},
            {"test_case_id": "TC-003", "title": "SMS failure triggers alternate phone communication", "expected_results": ["A phone communication is recorded after the SMS failure."], "requirement_id": "BR-006", "review_status": "approved", "execution_status": "not_run"},
            {"test_case_id": "TC-004", "title": "Communication outcome is recorded in the audit log", "expected_results": ["The communication outcome is persisted with the test case evidence."], "requirement_id": "BR-007", "review_status": "approved", "execution_status": "not_run"},
        ]
        contract = {
            "contract_version": "demo-1.0",
            "generation_id": GENERATION_ID,
            "analysis_id": analysis["analysis_id"],
            "package_id": package["package_id"],
            "project_id": project["project_id"],
            "test_cases": cases,
            "review_metadata": {"reviewer": "Product Owner", "status": "approved"},
        }
        created = iso(now - timedelta(days=1))
        conn.execute(
            "INSERT INTO test_case_generation (generation_id,analysis_id,package_id,project_id,status,contract_json,model_name,prompt_version,prompt_hash,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (GENERATION_ID, analysis["analysis_id"], package["package_id"], project["project_id"], "completed", json_text(contract), "demo-seed", "demo-results-v1", "demo", created, created),
        )

        run_specs = [
            (DEMO_RUN_IDS[0], now - timedelta(hours=30), {"TC-001": "passed", "TC-002": "passed", "TC-003": "passed", "TC-004": "passed"}, "Baseline completed successfully."),
            (DEMO_RUN_IDS[1], now - timedelta(hours=18), {"TC-001": "passed", "TC-002": "passed", "TC-003": "failed", "TC-004": "passed"}, "TC-003 failed alternate-channel validation."),
            (DEMO_RUN_IDS[2], now - timedelta(hours=6), {"TC-001": "passed", "TC-002": "failed", "TC-003": "passed", "TC-004": "passed"}, "TC-002 returned an unexpected notification."),
        ]
        for run_id, started, statuses, summary in run_specs:
            started_at = iso(started)
            completed_at = iso(started + timedelta(minutes=7))
            selected_ids = list(statuses)
            frozen = {**contract, "selected_test_case_ids": selected_ids, "selected_test_data_record_ids": [], "frozen_at": started_at, "frozen_by": "Product Owner"}
            baseline_hash = hashlib.sha256(json_text(frozen).encode()).hexdigest()
            config = {"generation_id": GENERATION_ID, "baseline_hash": baseline_hash, "selected_test_case_ids": selected_ids, "selected_record_count": 0, "reviewer": "Product Owner"}
            observations = [{"type": "baseline_frozen", "message": "Demo baseline seeded for Test Results validation.", "created_at": started_at}, {"type": "execution_completed", "message": summary, "created_at": completed_at}]
            conn.execute(
                "INSERT INTO test_run (run_id,package_id,project_id,status,configuration_snapshot,generation_id,selected_test_case_ids_json,frozen_contract_json,baseline_hash,reviewer,review_comments,lifecycle_state,approved_at,started_at,completed_at,immutable_observations_json,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (run_id, package["package_id"], project["project_id"], "completed", json_text(config), GENERATION_ID, json_text(selected_ids), json_text(frozen), baseline_hash, "Product Owner", "Demo run", "completed", started_at, started_at, completed_at, json_text(observations), started_at),
            )
            for case in cases:
                case_id = case["test_case_id"]
                status = statuses[case_id]
                actual = {"validation_location": "Gold layer", "observed": "Expected outcome confirmed" if status == "passed" else "Observed outcome differs from expected"}
                assertions = [{"name": "Outcome matches expected result", "status": status, "message": "Assertion passed." if status == "passed" else summary, "expected": case["expected_results"][0], "actual": actual["observed"]}]
                error = None if status == "passed" else summary
                result_id = f"{run_id}_{case_id}"
                conn.execute(
                    "INSERT INTO test_execution_result (result_id,run_id,generation_id,test_case_id,status,expected_json,actual_json,assertions_json,bronze_refs_json,started_at,completed_at,error) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (result_id, run_id, GENERATION_ID, case_id, status, json_text({"expected_results": case["expected_results"]}), json_text(actual), json_text(assertions), json_text([]), started_at, completed_at, error),
                )
    print(f"Seeded {len(DEMO_RUN_IDS)} demo runs for Test Project 5 ({GENERATION_ID}).")


if __name__ == "__main__":
    main()
