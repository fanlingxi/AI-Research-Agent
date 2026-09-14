import copy
import json

import pytest

from app.agent.research_workflow import ResearchDraft
from app.agent.semantic_review import SemanticReviewService
from app.benchmarking.semantic_review import observe
from app.context.models import canonical_package_sha256
from app.experiments.calibration import annotation_pack, calibrate
from tests.test_research_workflow import _draft, _research_stack


@pytest.fixture
def inputs(tmp_path):
    _, _, _, _, _, package, bundle, _ = _research_stack(tmp_path, [])
    draft = ResearchDraft.model_validate(
        _draft(bundle.claim.claim_id, bundle.evidence[0].evidence_id, valid=True)
    )
    return draft, package, package.package_sha256


class Judge:
    last_usage = {"input_tokens": 1, "output_tokens": 1}

    def __init__(self, transform=None):
        self.transform, self.calls = transform, 0

    def invoke(self, prompt, system_prompt=None):
        self.calls += 1
        findings = json.loads(prompt)["findings"]
        response = {
            "findings": [
                {
                    "finding_id": f["finding_id"],
                    "verdict": "supported",
                    "reason": "Fixture response, not a quality judgment.",
                    "proofs": [
                        {
                            "evidence_id": f["citations"][0]["evidence_id"],
                            "quote": f["citations"][0]["quote"],
                        }
                    ],
                }
                for f in findings
            ]
        }
        if self.transform:
            self.transform(response)
        return json.dumps(response)


def test_all_four_verdicts_preserve_locations_and_do_not_mutate_inputs(inputs):
    draft, package, sha = inputs
    draft.findings *= 4
    before = package.model_dump_json(), draft.model_dump_json()
    labels = ["supported", "contradicted", "insufficient_evidence", "cannot_determine"]

    def change(response):
        for finding, verdict in zip(response["findings"], labels, strict=True):
            finding["verdict"] = verdict

    result = SemanticReviewService().review(draft, package, sha, Judge(change), model="fixture")
    assert result["status"] == "completed"
    assert [f["verdict"] for f in result["findings"]] == labels
    assert result["full_report_semantic_success"] is None
    assert result["findings"][0]["citations"][0]["page_start"] == 1
    assert result["findings"][0]["proofs"][0]["quote_start"] == 0
    assert before == (package.model_dump_json(), draft.model_dump_json())


def test_hard_citation_failure_prevents_model_call(inputs):
    draft, package, sha = inputs
    draft.findings[0].evidence_ids = ["foreign-evidence"]
    judge = Judge()
    result = SemanticReviewService().review(draft, package, sha, judge, model="fixture")
    assert result["status"] == "hard_validation_failed" and judge.calls == 0


def test_snapshot_and_empty_scope_rejected_before_model(inputs):
    draft, package, sha = inputs
    judge = Judge()
    package.knowledge.claim_bundles[0].evidence[0].quote = "changed"
    with pytest.raises(ValueError, match="fingerprint"):
        SemanticReviewService().review(draft, package, sha, judge, model="fixture")
    package.constraints.collection_scopes = []
    package.package_sha256 = canonical_package_sha256(package)
    with pytest.raises(ValueError, match="scope"):
        SemanticReviewService().review(
            draft, package, package.package_sha256, judge, model="fixture"
        )
    assert judge.calls == 0


@pytest.mark.parametrize("mode", ["foreign", "fabricated", "missing", "blank"])
def test_invalid_judge_quotes_cannot_become_supported(inputs, mode):
    def change(response):
        proof = response["findings"][0]["proofs"][0]
        if mode == "foreign":
            proof["evidence_id"] = "foreign-id"
        elif mode == "fabricated":
            proof["quote"] = "invented quote"
        elif mode == "blank":
            proof["quote"] = " "
        else:
            response["findings"][0]["proofs"] = []

    result = SemanticReviewService().review(*inputs, Judge(change), model="fixture")
    assert result["status"] == "partial"
    assert result["findings"][0]["verdict"] == "cannot_determine"
    assert not result["findings"][0]["proofs"]


@pytest.mark.parametrize("mode", ["omitted", "duplicate", "unknown"])
def test_judge_must_cover_exact_finding_set(inputs, mode):
    def change(response):
        if mode == "omitted":
            response["findings"] = []
        elif mode == "duplicate":
            response["findings"] *= 2
        else:
            response["findings"][0]["finding_id"] = "other-finding"

    result = SemanticReviewService().review(*inputs, Judge(change), model="fixture")
    assert result["status"] == "judge_failed"
    assert result["findings"][0]["status"] == "judge_failed"


def test_provider_failure_retained_without_secret_or_retry(inputs):
    def fail(_):
        raise TimeoutError("api_key=secret-do-not-log")

    judge = Judge(fail)
    result = SemanticReviewService().review(*inputs, judge, model="fixture")
    assert result["status"] == "judge_failed" and judge.calls == 1
    assert "secret" not in json.dumps(result)


def test_large_evidence_is_not_silently_truncated(inputs):
    draft, package, sha = inputs
    draft.findings *= 30
    for finding in draft.findings:
        finding.assertion = "long" * 1000
    judge = Judge()
    result = SemanticReviewService().review(draft, package, sha, judge, model="fixture")
    assert result["status"] == "input_limit" and judge.calls == 0
    assert len(result["findings"]) == 30


def test_blind_labels_calibration_denominators_and_binding(inputs):
    review = SemanticReviewService().review(*inputs, Judge(), model="fixture")
    records = [
        {"task_id": "q1", "status": "completed", "review": review},
        {"task_id": "q2", "status": "source_not_completed"},
    ]
    labels = annotation_pack(records)
    assert "verdict" not in json.dumps(labels) and labels["cases"][0]["label"] is None
    blank = calibrate(records, labels)
    assert blank["planned_runs"] == 2 and blank["agreement_on_labeled"] is None
    assert blank["false_support_count"] is None and not blank["publication_gate_enabled"]
    labels["cases"][0].update(
        label="contradicted", reviewer="test-human", note="Synthetic test only"
    )
    measured = calibrate(records, labels)
    assert measured["false_support_rate"] == 1 and measured["human_labeled"] == 1
    with pytest.raises(ValueError, match="Duplicate observation"):
        calibrate(records + [records[0]], labels)
    labels["cases"][0]["assertion"] = "tampered"
    with pytest.raises(ValueError, match="changed"):
        calibrate(records, labels)


def test_archive_input_change_discards_judgment_and_reentry_does_not_overwrite(
    inputs, tmp_path, monkeypatch
):
    calls = []

    def load(*_):
        calls.append(1)
        return {
            "task_id": "q1",
            "status": "prepared",
            "input_files_sha256": str(len(calls)),
        }, inputs

    monkeypatch.setattr("app.benchmarking.semantic_review.load_case", load)
    monkeypatch.setattr("app.benchmarking.semantic_review.code_fingerprint", lambda: {})
    output = tmp_path / "observation"
    records = observe(None, "fixture", ["q1"], output, Judge(), "fixture")
    assert records[0]["status"] == "input_changed" and "review" not in records[0]
    assert (output / "q1/rejected-review.json").exists()
    before = (output / "results.json").read_bytes()
    with pytest.raises(FileExistsError):
        observe(None, "fixture", ["q1"], output)
    assert (output / "results.json").read_bytes() == before


def test_missing_judgment_is_abstention_in_calibration(inputs):
    review = SemanticReviewService().prepare(*inputs)
    records = [{"task_id": "q1", "status": "prepared", "review": copy.deepcopy(review)}]
    labels = annotation_pack(records)
    labels["cases"][0].update(label="supported", reviewer="test-human", note="Synthetic only")
    result = calibrate(records, labels)
    assert result["judge_unavailable_on_labeled"] == 1
    assert result["missed_support_count"] == 1
