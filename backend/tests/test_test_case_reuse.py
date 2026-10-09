import json

import pytest

from app.test_case_reuse import normalized, requirement_text, similarity


def test_matching_is_based_on_functional_behavior_not_ids():
    first = {"requirement_id": "FR-001", "title": "Validate refill eligibility", "statement": "Only eligible members receive a refill reminder", "acceptance_criteria": [{"given": "eligible member", "when": "reminder is due", "then": "send SMS"}], "business_rules": []}
    second = {**first, "requirement_id": "FR-010"}
    unrelated = {"statement": "Calculate daily sales totals"}
    assert requirement_text(first) == requirement_text(second)
    assert similarity(requirement_text(first), requirement_text(second)) == 1
    assert similarity(requirement_text(first), requirement_text(unrelated)) < 0.55
    assert normalized("Eligible-member") == "eligible member"


@pytest.mark.parametrize("requirement_count", [1, 4, 10, 12])
def test_case_count_follows_actual_requirements_and_distinct_conditions(requirement_count):
    from app.main import _test_case_coverage_plan, _validate_requirement_case_mapping

    requirements = [{"requirement_id": f"FR-{index:03d}",
                     "acceptance_criteria": [{"acceptance_id": f"AC-{index:03d}", "given": "Eligible member", "when": "Reminder is due", "then": "Send SMS reminder"}],
                     "business_rules": []} for index in range(1, requirement_count + 1)]
    plan, gaps = _test_case_coverage_plan(requirements)
    assert len(plan) == requirement_count
    assert not gaps
    assert [item["requirement"]["requirement_id"] for item in plan] == [item["requirement_id"] for item in requirements]
    cases = [{"test_case_id": f"TC-{index:03d}", "requirement_id": item["requirement_id"], "title": "Verify SMS reminder",
              "steps": ["Send SMS reminder for eligible member when due"], "expected_results": ["SMS reminder sent"],
              "acceptance_criteria_ids": [item["acceptance_criteria"][0]["acceptance_id"]]} for index, item in enumerate(requirements, 1)]
    _validate_requirement_case_mapping(cases, requirements)
    # A second documented condition needs its own coverage, not a fixed 10-case cap.
    next_id = f"AC-{requirement_count + 1:03d}"
    requirements[0]["acceptance_criteria"].append({"acceptance_id": next_id, "given": "Ineligible member", "when": "Reminder is due", "then": "Suppress SMS reminder"})
    expanded, _ = _test_case_coverage_plan(requirements)
    assert len(expanded) == requirement_count + 1
    cases.append({"test_case_id": f"TC-{requirement_count + 1:03d}", "requirement_id": "FR-001", "title": "Verify reminder suppression", "steps": ["Suppress SMS reminder for ineligible member"],
                  "expected_results": ["SMS reminder suppressed"], "acceptance_criteria_ids": [next_id]})
    _validate_requirement_case_mapping(cases, requirements)
    if requirement_count > 1:
        cases[-2]["requirement_id"] = "FR-001"
    else:
        cases[-2]["requirement_id"] = "FR-999"
    try:
        _validate_requirement_case_mapping(cases, requirements)
    except ValueError:
        pass
    else:
        raise AssertionError("A missing FR mapping must be rejected")


def test_reuse_preview_distinguishes_verified_copy_from_adaptation():
    from app.main import _case_reuse_preview

    requirement = {"requirement_id": "FR-001", "title": "Validate refill eligibility", "statement": "Only eligible members receive a refill reminder",
                   "acceptance_criteria": [{"acceptance_id": "AC-001", "given": "Eligible member", "when": "Reminder is due", "then": "Send SMS reminder"}], "business_rules": []}
    current_text = requirement_text(requirement)
    case = {"test_case_id": "TC-001", "requirement_id": "FR-001", "title": "Verify SMS refill reminder", "steps": ["Send SMS reminder for eligible member when due"], "expected_results": ["SMS reminder sent"], "acceptance_criteria_ids": ["AC-001"], "business_rule_ids": []}
    candidates = [{"candidate_id": "old:TC-001", "origin": "history", "requirement_text": current_text, "requirement": requirement,
                   "case": case, "same_revisions": True},
                  {"candidate_id": "external-1", "origin": "external", "requirement_text": current_text, "case": {**case, "title": "Check membership for reminder"}, "same_revisions": False}]
    preview = _case_reuse_preview([requirement], candidates)[0]
    assert preview["suggested_action"] == "reuse"
    assert preview["matches"][0]["candidate_id"] == "old:TC-001"
    assert preview["matches"][0]["action"] == "reuse"
    # An external case may guide new generation, but cannot carry old citations.
    assert preview["matches"][1]["action"] == "adapt_with_llm"


def test_coverage_summary_requires_behavior_not_just_citation():
    from app.main import _build_test_case_coverage_review

    requirement = {"requirement_id": "FR-001", "title": "Send a refill reminder",
                   "acceptance_criteria": [{"acceptance_id": "AC-001", "given": "Eligible member",
                                            "when": "Reminder is due", "then": "Send SMS reminder"}],
                   "business_rules": [{"rule_id": "BR-001", "text": "Send SMS reminder only to eligible members"}]}
    case = {"test_case_id": "TC-001", "requirement_id": "FR-001",
            "acceptance_criteria_ids": ["AC-001"], "business_rule_ids": ["BR-001"],
            "steps": ["Open the dashboard"], "expected_results": ["Dashboard opens"]}
    contract = {"test_cases": [case]}
    empty = _build_test_case_coverage_review(contract, [requirement])["summary"]
    assert (empty["requirements_covered"], empty["acceptance_criteria_covered"], empty["business_rules_covered"]) == (0, 0, 0)
    case["steps"] = ["Send SMS reminder to eligible member when reminder is due"]
    case["expected_results"] = ["SMS reminder is sent to eligible member"]
    covered = _build_test_case_coverage_review(contract, [requirement])["summary"]
    assert (covered["requirements_covered"], covered["acceptance_criteria_covered"], covered["business_rules_covered"]) == (1, 1, 1)


def test_historical_cases_are_scoped_to_selected_brds_not_shared_api_sources():
    from app.main import _project_case_candidates

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

    class Connection:
        def execute(self, sql, params=()):
            if "FROM source_package_item pi" in sql:
                brd = {"current": "brd-a", "unrelated": "brd-b", "related": "brd-a"}[params[0]]
                return Result([{"source_id": brd, "revision_id": f"{params[0]}-brd", "source_type": "brd"},
                               {"source_id": "shared-api", "revision_id": "api-rev", "source_type": "api_spec"}])
            if "FROM test_case_generation g" in sql:
                requirement = json.dumps({"requirements": [{"requirement_id": "FR-001", "title": "Send reminder", "statement": "Send a reminder"}]})
                contract = json.dumps({"test_cases": [{"test_case_id": "TC-001", "requirement_id": "FR-001", "title": "Reminder", "review_status": "approved"}]})
                return Result([{"generation_id": package, "package_id": package, "requirement_json": requirement, "contract_json": contract}
                               for package in ("unrelated", "related")])
            if "FROM external_test_case" in sql:
                return Result([])
            raise AssertionError(sql)

    candidates = _project_case_candidates(Connection(), "project", "current")
    assert [item["package_id"] for item in candidates] == ["related"]
