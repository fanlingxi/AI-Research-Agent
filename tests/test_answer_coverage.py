import json

import pytest

from app.agent.models import AgentRunCreateRequest
from app.agent.structured_research import (
    CoveredReport,
    ReportContractError,
    prepare_covered_report,
)
from app.context.models import ContextBuildRequest, canonical_package_sha256
from app.context.service import ContextBuilderService
from app.retrieval.coverage import CoverageReranker, coverage_indices
from app.retrieval.reranking import input_digest
from tests.test_a08_production import report
from tests.test_context_neighbors import _setup
from tests.test_research_workflow import _analysis, _plan, _research_stack


def anchored_report(bundle, question):
    value = report(bundle)
    value["findings"][0].update(
        scope="仅限所给证据",
        support=[
            {"evidence_id": bundle.evidence[0].evidence_id, "quote": bundle.evidence[0].quote}
        ],
    )
    value["answer_parts"] = [{"question_part": question, "finding_indices": [0], "gap": ""}]
    return value


@pytest.mark.parametrize(
    "workflow,version", [("research_v5", "structured-v4"), ("research_v6", "structured-v5")]
)
@pytest.mark.parametrize("invalid", ["quote", "part", "ownership", "indices", "gap", "duplicate"])
def test_anchored_worker_repairs_invalid_contract_without_reflecting_output(
    tmp_path, invalid, workflow, version
):
    repo, service, worker, project, task, _, bundle, llm = _research_stack(
        tmp_path, [_analysis(), _plan()]
    )
    value = anchored_report(bundle, task.goal)
    broken = json.loads(json.dumps(value))
    f = broken["findings"][0]
    if invalid == "quote":
        f["support"][0]["quote"] = "UNTRUSTED_REFLECTION_MARKER_123"
    elif invalid == "part":
        broken["answer_parts"][0]["question_part"] = "UNTRUSTED_REFLECTION_MARKER_123"
    elif invalid == "ownership":
        f["claim_bundle_id"] = "different-bundle"
    elif invalid == "indices":
        broken["answer_parts"][0]["finding_indices"] = [0, 0]
    elif invalid == "gap":
        broken["answer_parts"][0]["gap"] = "Missing information."
    else:
        broken["findings"].append(dict(f))
        broken["answer_parts"][0]["finding_indices"] = [0, 1]
    llm.responses.extend([json.dumps(broken), json.dumps(value)])
    run = service.create_run(
        project.id,
        task.id,
        AgentRunCreateRequest(workflow=workflow, token_budget=32000, reading_format="inline-v1"),
    )
    assert worker.run_once()
    done = service.get_run(run.id)
    assert done.status == "completed", done.error_message
    assert done.repair_count == 1
    assert done.workflow_version == version
    output = service.repository.get_output(run.id)
    assert "适用范围：仅限所给证据" in output.rendered_text
    assert output.rendered_text.count(f"[cite:{bundle.evidence[0].evidence_id}]") == 1
    assert "UNTRUSTED_REFLECTION_MARKER" not in llm.calls[-1]
    with repo.database.connect() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM research_generation_attempts WHERE run_id=?", (run.id,)
            ).fetchone()[0]
            == 2
        )


def test_support_anchor_is_not_semantic_approval_and_refusal_is_allowed(tmp_path):
    _, _, _, _, task, package, bundle, _ = _research_stack(tmp_path, [])
    value = anchored_report(bundle, task.goal)
    # Correct location does not establish entailment: never label this semantic success.
    value["findings"][0]["assertion"] = "A deliberately unsupported assertion."
    assert prepare_covered_report(CoveredReport.model_validate(value), package)
    value.update(
        answer_status="insufficient_evidence", findings=[], limitations=["Missing evidence"]
    )
    value["answer_parts"][0].update(finding_indices=[], gap="Missing evidence")
    draft = prepare_covered_report(CoveredReport.model_validate(value), package)
    assert not draft.findings and "证据缺口" in draft.markdown
    value["answer_parts"][0]["gap"] = ""
    with pytest.raises(ReportContractError):
        prepare_covered_report(CoveredReport.model_validate(value), package)


def test_coverage_prefers_breadth_and_rejects_unbound_or_fabricated_anchors():
    passages = [f"Evidence passage {i} with enough exact source characters." for i in range(4)]
    payload = {
        "question": "Explain methods and results",
        "candidates": [{"index": i, "passages": [p]} for i, p in enumerate(passages)],
    }
    parts = [
        {
            "question_part": "methods",
            "anchors": [{"index": i, "quote": passages[i]} for i in [0, 1]],
        },
        {"question_part": "results", "anchors": [{"index": 3, "quote": passages[3]}]},
    ]
    assert coverage_indices(payload, parts) == [0, 3, 1]
    repeated = json.loads(json.dumps(parts))
    repeated[0]["anchors"].insert(1, dict(repeated[0]["anchors"][0]))
    assert coverage_indices(payload, repeated) == [0, 3, 1]
    for key, bad in [("index", True), ("index", -1), ("index", 9), ("quote", passages[2])]:
        modified = json.loads(json.dumps(parts))
        modified[0]["anchors"][0][key] = bad
        with pytest.raises(ValueError):
            coverage_indices(payload, modified)


def test_coverage_failure_keeps_usage_and_falls_back():
    class Invalid:
        last_usage = {}

        def invoke(self, *args, **kwargs):
            self.last_usage = {"input_tokens": 100, "output_tokens": 20}
            return '{"parts":[]}'

    result = CoverageReranker(Invalid()).rank(
        {"question": "test", "candidates": [{"index": 0, "passages": ["some text"]}]}
    )
    assert result["status"] == "fallback" and result["model_calls"] == 1
    assert result["usage"]["input_tokens"] == 100


def test_live_trial_cannot_bypass_offline_isolation():
    from app.benchmarking.coverage_trial import run

    assert run()["mode"] == "dry_run"
    assert run(refine=True)["max_new_calls"] == 39
    with pytest.raises(ValueError, match="disabled in offline tests"):
        run(execute=True, refine=True)


class AnchoredRanker:
    def __init__(self, mutate=None):
        self.calls, self.mutate = 0, mutate

    def rank(self, payload):
        self.calls += 1
        parts = [
            {
                "question_part": payload["question"],
                "anchors": [
                    {
                        "index": len(payload["candidates"]) - 1,
                        "quote": payload["candidates"][-1]["passages"][0][:40],
                    }
                ],
            }
        ]
        if self.mutate:
            self.mutate()
        return {
            "status": "ranked",
            "parts": parts,
            "model_calls": 1,
            "indices": coverage_indices(payload, parts),
            "input_sha256": input_digest(payload),
        }


def test_coverage_revalidates_scope_and_preserves_old_snapshot(tmp_path):
    repo, task, claims = _setup(tmp_path)
    ranker = AnchoredRanker()
    builder = ContextBuilderService(repo, coverage_reranker=ranker)
    request = ContextBuildRequest(task_id=task.id, max_tokens=16000)
    old = builder.build_context(request)
    new = builder.build_context(request.model_copy(update={"evidence_reranking": "coverage-v1"}))
    assert (
        new.knowledge.claim_bundles[0].claim.claim_id
        == old.knowledge.claim_bundles[-1].claim.claim_id
    )
    observed = new.retrieval_audit.parameters["reranking"]["selected_coverage"][0]
    assert observed["selected_claim_ids"] == [new.knowledge.claim_bundles[0].claim.claim_id]
    assert observed["semantic_support"] == "unreviewed"
    assert (
        canonical_package_sha256(builder.snapshot_repository.get(old.snapshot_id))
        == old.package_sha256
    )

    def withdraw():
        with repo.database.connect() as db:
            db.execute("UPDATE claims SET status='withdrawn' WHERE id=?", (claims[0],))

    ranker.mutate = withdraw
    changed = builder.build_context(
        request.model_copy(update={"evidence_reranking": "coverage-v1"})
    )
    assert ranker.calls == 2
    audit = changed.retrieval_audit.parameters["reranking"]
    assert audit["reason"] == "inputs_changed_after_rerank"
    assert audit["selected_coverage"] == []
    assert claims[0] not in {b.claim.claim_id for b in changed.knowledge.claim_bundles}
