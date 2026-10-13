import json

import pytest

from app.knowledge.extractor import LiveLLMRequiredError
from app.knowledge.prereview import CandidatePrereviewer
from tests.test_knowledge_workflow import UnlocatedExtractor, _service


class Judge:
    """Fake reviewer: rejects one named entity, approves everything else it is shown."""

    provider_name = "fake"
    model = "judge-test"

    def __init__(self, reply=None) -> None:
        self.calls = 0
        self.reply = reply

    def invoke(self, prompt: str, system_prompt: str | None = None) -> str:
        self.calls += 1
        rows = json.loads(prompt.split("\n", 1)[1])
        if self.reply is not None:
            return self.reply(rows)
        return json.dumps([
            {"candidate_id": row["candidate_id"],
             "verdict": "reject" if row.get("name") == "科研任务自动化" else "approve",
             "reason": "测试理由"}
            for row in rows
        ], ensure_ascii=False)


def _ingest(service, name="paper.pdf"):
    ingestion = service.submit(topic="智能体工具使用", sources=[name], pdf_max_pages=4)
    service.run(ingestion.id)
    return ingestion.id


def _by_name(repository, ingestion_id):
    names = {}
    result = {}
    items = repository.list_candidates(ingestion_id)
    for item in items:
        if item["kind"] == "entity":
            names[item["candidate"]["id"]] = item["candidate"]["name"]
    for item in items:
        candidate = item["candidate"]
        key = candidate.get("name") or (
            names[candidate["source_candidate_id"]] + "->" + names[candidate["target_candidate_id"]]
        )
        result[key] = candidate
    return result


def test_prereview_applies_verdicts_and_keeps_unresolved_relations_for_humans(tmp_path):
    repository, service, _ = _service(tmp_path)
    ingestion_id = _ingest(service)
    service.llm = judge = Judge()

    result = CandidatePrereviewer(service).run(ingestion_id, limit=50)

    candidates = _by_name(repository, ingestion_id)
    assert candidates["工具增强智能体"]["status"] == "published"
    assert candidates["科研任务自动化"]["status"] == "rejected"
    # A relation to a rejected entity cannot be approved; it stays a draft for a human.
    blocked = candidates["工具增强智能体->科研任务自动化"]
    assert blocked["status"] == "draft"
    assert blocked["metadata"]["prereview"]["verdict"] == "unsure"
    assert result["rejected"] == 1 and result["unsure"] >= 1 and result["remaining"] == 0
    decisions = [item for item in repository.list_candidates(ingestion_id, status="rejected")]
    assert decisions[0]["candidate"]["metadata"]["prereview"]["reason"] == "测试理由"

    calls = judge.calls
    again = CandidatePrereviewer(service).run(ingestion_id)
    assert judge.calls == calls and again["items"] == [] and again["remaining"] == 0


def test_prereview_merges_into_exact_existing_entity(tmp_path):
    repository, service, _ = _service(tmp_path)
    first = _ingest(service, "first.pdf")
    service.llm = Judge()
    CandidatePrereviewer(service).run(first, limit=50)
    published = {item.name: item.id for item in repository.list_published_entities()}

    second = _ingest(service, "second.pdf")
    result = CandidatePrereviewer(service).run(second, limit=50)

    candidate = _by_name(repository, second)["工具增强智能体"]
    assert candidate["status"] == "merged"
    assert candidate["canonical_id"] == published["工具增强智能体"]
    assert result["merged"] >= 1


def test_unlocated_evidence_is_never_sent_or_approved(tmp_path):
    repository, service, _ = _service(tmp_path)
    service.extractor = UnlocatedExtractor()
    ingestion_id = _ingest(service)
    service.llm = judge = Judge()

    result = CandidatePrereviewer(service).run(ingestion_id, limit=50)

    assert judge.calls == 0
    assert result["approved"] == result["merged"] == result["rejected"] == 0
    assert all(item["candidate"]["status"] == "draft"
               for item in repository.list_candidates(ingestion_id))


def test_malformed_or_foreign_verdicts_leave_candidates_unsure(tmp_path):
    repository, service, _ = _service(tmp_path)
    ingestion_id = _ingest(service)
    foreign = Judge(lambda rows: json.dumps(
        [{"candidate_id": "someone-else", "verdict": "approve", "reason": "x"}]
    ))
    service.llm = foreign
    CandidatePrereviewer(service).run(ingestion_id, limit=3)
    service.llm = Judge(lambda rows: "not json")
    CandidatePrereviewer(service).run(ingestion_id, limit=50)

    items = repository.list_candidates(ingestion_id)
    assert all(item["candidate"]["status"] == "draft" for item in items)
    assert {item["candidate"]["metadata"]["prereview"]["verdict"] for item in items} == {"unsure"}


def test_prereview_requires_a_live_model(tmp_path):
    _, service, _ = _service(tmp_path)
    ingestion_id = _ingest(service)
    service.require_live_llm = True

    with pytest.raises(LiveLLMRequiredError):
        CandidatePrereviewer(service).run(ingestion_id)


def test_relation_endpoints_are_named_even_after_entities_were_decided(tmp_path):
    repository, service, _ = _service(tmp_path)
    ingestion_id = _ingest(service)
    seen = []

    def remember(rows):
        seen.extend(rows)
        return json.dumps([{"candidate_id": row["candidate_id"], "verdict": "approve",
                            "reason": "ok"} for row in rows])

    service.llm = Judge(remember)
    CandidatePrereviewer(service).run(ingestion_id, limit=50)

    relations = [row for row in seen if row["kind"] == "关系"]
    assert relations and all(row["source"] and row["target"] for row in relations)
