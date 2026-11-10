import json

import pytest

from app.knowledge.extractor import LiveLLMRequiredError
from app.knowledge.prereview import (
    CONTEXT_LIMIT,
    PASSAGE_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    CandidatePrereviewer,
    build_review_row,
    passage_context,
)
from app.knowledge.review_content import review_row
from tests.test_knowledge_workflow import UnlocatedExtractor, _service


class Judge:
    """Fake reviewer: rejects one named entity, approves everything else it is shown."""

    provider_name = "fake"
    model = "judge-test"

    def __init__(self, reply=None) -> None:
        self.calls = 0
        self.reply = reply
        self.prompts = []

    def invoke(self, prompt: str, system_prompt: str | None = None) -> str:
        self.calls += 1
        self.prompts.append((prompt, system_prompt))
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

    result = CandidatePrereviewer(service, auto_reject=True).run(ingestion_id, limit=50)

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


@pytest.mark.parametrize("tiered", [False, True])
def test_rejection_is_a_suggestion_by_default_and_is_not_reviewed_again(
    tmp_path, monkeypatch, tiered
):
    repository, service, _ = _service(tmp_path)
    ingestion_id = _ingest(service)
    service.llm = judge = Judge()
    second = Judge() if tiered else None
    decide = service.decide

    def allow_only_approvals(candidate_id, decision, **kwargs):
        assert decision.decision != "reject"
        return decide(candidate_id, decision, **kwargs)

    monkeypatch.setattr(service, "decide", allow_only_approvals)
    assert service.settings.prereview_auto_reject is False
    reviewer = CandidatePrereviewer(service, second_llm=second, spot_check_rate=0)
    result = reviewer.run(ingestion_id, limit=50)

    candidate = _by_name(repository, ingestion_id)["科研任务自动化"]
    assert candidate["status"] == "draft"
    review = candidate["metadata"]["prereview"]
    assert review["verdict"] == "reject" and review["reason"] == "测试理由"
    assert result["suggested_reject"] == 1 and result["rejected"] == 0
    item = next(item for item in result["items"] if item["candidate_id"] == candidate["id"])
    assert item["verdict"] == "suggested_reject" and item["reason"] == "测试理由"
    assert repository.get_review_decision(candidate["id"]) is None

    calls = judge.calls
    second_calls = second.calls if second else 0
    again = reviewer.run(ingestion_id)
    assert judge.calls == calls and (second.calls if second else 0) == second_calls
    assert again["items"] == [] and again["remaining"] == 0
    assert again["suggested_reject"] == 0


@pytest.mark.parametrize("tiered", [False, True])
@pytest.mark.parametrize("setting,override,outcome", [
    (True, None, "rejected"),
    (True, False, "suggested_reject"),
    (False, True, "rejected"),
])
def test_auto_reject_uses_settings_unless_explicitly_overridden(
    tmp_path, tiered, setting, override, outcome
):
    repository, service, _ = _service(tmp_path)
    ingestion_id = _ingest(service)
    service.settings.prereview_auto_reject = setting
    service.llm = Judge(_verdicts(lambda row: "reject"))
    second = Judge(_verdicts(lambda row: "reject")) if tiered else None

    result = CandidatePrereviewer(service, second_llm=second, auto_reject=override).run(
        ingestion_id, limit=1,
    )

    assert result[outcome] == 1 and result["items"][0]["verdict"] == outcome
    other = "suggested_reject" if outcome == "rejected" else "rejected"
    assert result[other] == 0
    candidate_id = result["items"][0]["candidate_id"]
    candidate = repository.get_candidate(candidate_id)["candidate"]
    assert candidate["status"] == ("rejected" if outcome == "rejected" else "draft")
    assert candidate["metadata"]["prereview"]["verdict"] == "reject"


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


@pytest.mark.parametrize("verdict", ["approve", "reject"])
@pytest.mark.parametrize("edit_at", ["invoke", "decide"])
def test_edited_candidate_stays_draft_and_can_be_reviewed_again(
    tmp_path, monkeypatch, verdict, edit_at
):
    repository, service, _ = _service(tmp_path)
    ingestion_id = _ingest(service)
    target = repository.list_candidates(ingestion_id, status="draft")[0]["candidate"]
    candidate_id = target["id"]
    edited_summary = target["summary"] + "（审核期间修订）"
    seen = []

    def reply(rows):
        seen.extend(rows)
        if edit_at == "invoke":
            repository.update_candidate(candidate_id, {"summary": edited_summary})
        return json.dumps([{"candidate_id": row["candidate_id"], "verdict": verdict,
                            "reason": "测试理由"} for row in rows])

    if edit_at == "decide":
        decide = service.decide

        def edit_before_decision(*args, **kwargs):
            # After _settle's comparison and metadata write, before BEGIN IMMEDIATE.
            repository.update_candidate(candidate_id, {"summary": edited_summary})
            return decide(*args, **kwargs)

        monkeypatch.setattr(service, "decide", edit_before_decision)
    service.llm = Judge(reply)

    result = CandidatePrereviewer(service, auto_reject=True).run(ingestion_id, limit=1)

    assert seen[0]["summary"] == target["summary"]
    assert result["unsure"] == 1
    assert result["approved"] == result["merged"] == result["rejected"] == 0
    assert result["items"][0]["reason"] == "审核期间候选已被修改，需重新审核。"
    current = repository.get_candidate(candidate_id)["candidate"]
    assert current["status"] == "draft" and current["summary"] == edited_summary
    assert current["metadata"]["prereview"]["verdict"] == "unsure"
    assert current["metadata"]["prereview"]["retry_required"] is True
    assert result["remaining"] == len(repository.list_candidates(ingestion_id, status="draft"))
    assert repository.list_published_entities() == []
    assert repository.list_published_relations() == []
    assert repository.get_review_decision(candidate_id) is None
    assert repository.claim_projection() is None

    monkeypatch.undo()
    seen.clear()

    def approve_current(rows):
        seen.extend(rows)
        return json.dumps([{"candidate_id": row["candidate_id"], "verdict": "approve",
                            "reason": "修订后通过"} for row in rows])

    service.llm = Judge(approve_current)
    again = CandidatePrereviewer(service).run(ingestion_id, limit=1)
    assert seen[0]["summary"] == edited_summary
    assert again["approved"] == 1
    current = repository.get_candidate(candidate_id)["candidate"]
    assert current["status"] == "published"
    assert not current["metadata"]["prereview"].get("retry_required")
    assert repository.list_published_entities()[0].summary == edited_summary


def test_edit_before_merge_transaction_does_not_change_existing_entity(tmp_path, monkeypatch):
    repository, service, _ = _service(tmp_path)
    first = _ingest(service, "first.pdf")
    service.llm = Judge()
    CandidatePrereviewer(service).run(first, limit=50)
    before = [entity.model_dump() for entity in repository.list_published_entities()]
    second = _ingest(service, "second.pdf")
    decide = service.decide

    def edit_before_merge(candidate_id, decision, **kwargs):
        assert decision.decision == "merge"
        repository.update_candidate(candidate_id, {"summary": "未经审核的修订"})
        return decide(candidate_id, decision, **kwargs)

    monkeypatch.setattr(service, "decide", edit_before_merge)
    result = CandidatePrereviewer(service).run(second, limit=1)

    assert result["unsure"] == 1 and result["merged"] == 0
    candidate_id = result["items"][0]["candidate_id"]
    assert repository.get_candidate(candidate_id)["candidate"]["status"] == "draft"
    assert repository.get_review_decision(candidate_id) is None
    assert before == [entity.model_dump() for entity in repository.list_published_entities()]


@pytest.mark.parametrize("field", ["summary", "source", "target"])
@pytest.mark.parametrize("edit_at", ["invoke", "decide"])
def test_relation_review_checks_summary_and_live_endpoint_names(
    tmp_path, monkeypatch, field, edit_at
):
    repository, service, _ = _service(tmp_path)
    ingestion_id = _ingest(service)
    service.approve_ready(ingestion_id)
    relation = next(item["candidate"] for item in repository.list_candidates(ingestion_id)
                    if item["kind"] == "relation")

    def edit():
        if field == "summary":
            repository.update_candidate(relation["id"], {"summary": "已修改的关系摘要"})
        else:
            # Published endpoints cannot be edited through the draft API. Simulate
            # a concurrent name write to verify the transaction reads live names.
            endpoint_id = relation[f"{field}_candidate_id"]
            with repository.database.connect() as connection:
                connection.execute(
                    "UPDATE candidates SET payload_json = "
                    "json_set(payload_json, '$.name', ?) WHERE id = ?",
                    ("已修改的端点名称", endpoint_id),
                )

    def reply(rows):
        if edit_at == "invoke":
            edit()
        return json.dumps([{"candidate_id": row["candidate_id"], "verdict": "approve",
                            "reason": "测试理由"} for row in rows])

    if edit_at == "decide":
        decide = service.decide

        def edit_before_decision(*args, **kwargs):
            edit()
            return decide(*args, **kwargs)

        monkeypatch.setattr(service, "decide", edit_before_decision)
    service.llm = Judge(reply)

    result = CandidatePrereviewer(service).run(ingestion_id, limit=50)

    assert result["unsure"] == 1 and result["approved"] == 0
    current = repository.get_candidate(relation["id"])["candidate"]
    assert current["status"] == "draft"
    assert current["metadata"]["prereview"]["retry_required"] is True
    assert result["remaining"] == 1
    assert repository.list_published_relations() == []
    assert repository.get_review_decision(relation["id"]) is None


def test_non_model_fields_can_change_without_invalidating_review(tmp_path):
    repository, service, _ = _service(tmp_path)
    ingestion_id = _ingest(service)

    def reply(rows):
        for row in rows:
            repository.update_candidate(row["candidate_id"], {"confidence": 0.5})
        return json.dumps([{"candidate_id": row["candidate_id"], "verdict": "approve",
                            "reason": "测试理由"} for row in rows])

    service.llm = Judge(reply)
    result = CandidatePrereviewer(service).run(ingestion_id, limit=1)

    assert result["approved"] == 1 and result["unsure"] == 0
    current = repository.get_candidate(result["items"][0]["candidate_id"])["candidate"]
    assert current["status"] == "published" and current["confidence"] == 0.5


@pytest.mark.parametrize("field", ["name", "type", "evidence.quote"])
def test_review_invalidated_by_other_model_visible_entity_fields(tmp_path, field):
    repository, service, _ = _service(tmp_path)
    ingestion_id = _ingest(service)

    def reply(rows):
        candidate_id = rows[0]["candidate_id"]
        # Type and evidence are extraction fields, so simulate their replacement
        # directly; a name edit uses the normal user-facing draft edit path.
        if field == "name":
            repository.update_candidate(candidate_id, {"name": "修订后的名称"})
        else:
            with repository.database.connect() as connection:
                connection.execute(
                    "UPDATE candidates SET payload_json = json_set(payload_json, ?, ?) "
                    "WHERE id = ?",
                    (f"$.{field}", "Method" if field == "type" else "新证据引文", candidate_id),
                )
        return json.dumps([{"candidate_id": candidate_id, "verdict": "approve", "reason": "ok"}])

    service.llm = Judge(reply)
    result = CandidatePrereviewer(service).run(ingestion_id, limit=1)

    assert result["unsure"] == 1 and result["approved"] == 0 and result["remaining"] > 0
    current = repository.get_candidate(result["items"][0]["candidate_id"])["candidate"]
    assert current["status"] == "draft"
    assert current["metadata"]["prereview"]["retry_required"] is True
    assert repository.list_published_entities() == []


def _verdicts(verdict_for):
    def reply(rows):
        return json.dumps([
            {"candidate_id": row["candidate_id"], "verdict": verdict_for(row), "reason": "理由"}
            for row in rows
        ], ensure_ascii=False)
    return reply


def test_tiered_prereview_decides_only_when_both_models_agree(tmp_path):
    repository, service, _ = _service(tmp_path)
    ingestion_id = _ingest(service)
    service.llm = first = Judge(_verdicts(lambda row: "approve"))
    # The second model rejects one entity the first approved and agrees on the rest.
    second = Judge(_verdicts(
        lambda row: "reject" if row.get("name") == "科研任务自动化" else "approve"
    ))

    result = CandidatePrereviewer(service, second_llm=second, spot_check_rate=0).run(
        ingestion_id, limit=50
    )

    candidates = _by_name(repository, ingestion_id)
    assert candidates["工具增强智能体"]["status"] == "published"
    disputed = candidates["科研任务自动化"]
    assert disputed["status"] == "draft"
    review = disputed["metadata"]["prereview"]
    assert review["verdict"] == "unsure" and "意见不一致" in review["reason"]
    assert "批准" in review["reason"] and "驳回" in review["reason"]
    assert review["model"] == "judge-test + judge-test"
    assert result["disagreed"] == 1 and result["rejected"] == 0 and result["spot_check"] == 0
    assert first.calls == second.calls and result["llm_calls"] == first.calls + second.calls


def test_spot_checked_agreed_approvals_stay_drafts_for_a_human(tmp_path):
    repository, service, _ = _service(tmp_path)
    ingestion_id = _ingest(service)
    service.llm = Judge(_verdicts(lambda row: "approve"))
    second = Judge(_verdicts(lambda row: "approve"))

    result = CandidatePrereviewer(service, second_llm=second, spot_check_rate=1.0).run(
        ingestion_id, limit=50
    )

    drafts = repository.list_candidates(ingestion_id, status="draft")
    entities = [item["candidate"] for item in drafts if item["kind"] == "entity"]
    assert entities and result["approved"] == 0 and result["spot_check"] == len(entities)
    assert not repository.list_candidates(ingestion_id, status="published")
    for candidate in entities:
        review = candidate["metadata"]["prereview"]
        assert review["verdict"] == "approve" and review["spot_check"] is True
        assert review["reason"].startswith("随机抽检")
    # A rerun must not reroll held items out of the spot check.
    again = CandidatePrereviewer(service, second_llm=second, spot_check_rate=1.0).run(ingestion_id)
    assert again["items"] == [] and again["remaining"] == 0


def test_spot_check_applies_only_with_a_second_model(tmp_path):
    repository, service, _ = _service(tmp_path)
    ingestion_id = _ingest(service)
    service.llm = Judge()

    result = CandidatePrereviewer(service, spot_check_rate=1.0).run(ingestion_id, limit=50)

    assert result["spot_check"] == 0 and result["approved"] >= 1


def test_default_prereview_prompt_is_byte_identical(tmp_path):
    repository, service, _ = _service(tmp_path)
    ingestion_id = _ingest(service)
    item = repository.list_candidates(ingestion_id, status="draft")[0]
    expected = "请逐条审核以下候选（JSON）：\n" + json.dumps(
        [review_row(item, {})], ensure_ascii=False,
    )
    service.llm = judge = Judge()

    CandidatePrereviewer(service).run(ingestion_id, limit=1)

    assert judge.prompts == [(expected, SYSTEM_PROMPT)]
    assert "evidence_context" not in expected
    metadata = repository.get_candidate(item["candidate"]["id"])["candidate"]["metadata"]
    assert "context" not in metadata["prereview"]


def test_passage_mode_from_settings_shares_context_between_reviewers(tmp_path):
    repository, service, _ = _service(tmp_path)
    ingestion_id = _ingest(service)
    item = repository.list_candidates(ingestion_id, status="draft")[0]
    service.settings.prereview_context_mode = "passage-v1"
    service.llm = first = Judge()
    second = Judge()

    CandidatePrereviewer(service, second_llm=second, spot_check_rate=0).run(
        ingestion_id, limit=1,
    )

    assert first.prompts == second.prompts
    prompt, system = first.prompts[0]
    assert system == PASSAGE_SYSTEM_PROMPT
    row = json.loads(prompt.split("\n", 1)[1])[0]
    assert row["evidence_quote"] in row["evidence_context"]
    assert len(row["evidence_context"]) <= CONTEXT_LIMIT
    metadata = repository.get_candidate(item["candidate"]["id"])["candidate"]["metadata"]
    assert metadata["prereview"]["context"] == "passage-v1"


@pytest.mark.parametrize("mismatch", ["quote", "paper", "chunk", "ingestion"])
def test_passage_mode_omits_unverified_context(tmp_path, mismatch):
    repository, service, _ = _service(tmp_path)
    ingestion_id = _ingest(service)
    item = repository.list_candidates(ingestion_id, status="draft")[0]
    candidate = item["candidate"]
    evidence = candidate["evidence"]
    if mismatch == "ingestion":
        # A valid chunk/paper pair from another ingestion is still out of scope.
        other_ingestion = _ingest(service, "other.pdf")
        other = repository.list_candidates(other_ingestion)[0]["candidate"]["evidence"]
        evidence.update(paper_id=other["paper_id"], chunk_id=other["chunk_id"])
    else:
        key = {"quote": "quote", "paper": "paper_id", "chunk": "chunk_id"}[mismatch]
        evidence[key] = "This quote or source does not exist in the original chunk."
    with repository.database.connect() as connection:
        connection.execute(
            "UPDATE candidates SET payload_json = json_set(payload_json, '$.evidence', "
            "json(?)) WHERE id = ?",
            (json.dumps(evidence), candidate["id"]),
        )
    item = repository.get_candidate(candidate["id"])
    service.llm = judge = Judge(_verdicts(lambda row: "unsure"))

    CandidatePrereviewer(service, context_mode="passage-v1").run(ingestion_id, limit=1)

    prompt, system = judge.prompts[0]
    assert system == PASSAGE_SYSTEM_PROMPT
    assert json.loads(prompt.split("\n", 1)[1]) == [review_row(item, {})]


@pytest.mark.parametrize("prefix,suffix", [(0, 5000), (3000, 3000), (5000, 0)])
def test_long_passage_window_contains_quote(prefix, suffix):
    quote = "The method lets an agent use tools to plan and solve research tasks."
    text = "前" * prefix + quote + "后" * suffix
    context = passage_context(quote, text)
    assert context is not None and quote in context
    assert len(context) == CONTEXT_LIMIT


def test_passage_anchor_only_tolerates_existing_layout_noise():
    quote = "The retrieval method uses external tools."
    text = "Before. The re-\ntrieval method uses external tools. After."
    assert passage_context(quote, text) == text
    assert passage_context("The method uses retrieval external tools.", text) is None
    assert passage_context("The retrieval method ... external tools.", text) is None
    assert passage_context("", text) is None
    assert passage_context("tiny", "tiny") is None


def test_passage_row_does_not_change_fingerprint_content():
    item = {"kind": "entity", "candidate": {
        "id": "test", "name": "方法", "type": "Method", "summary": "摘要",
        "evidence": {"quote": "The method uses external tools."},
    }}
    original = review_row(item, {})
    row = build_review_row(item, {}, context_mode="passage-v1", chunk_text="Other text.")
    assert row == original
    assert review_row(item, {}) == original


def test_passage_checks_the_quote_beyond_the_display_limit():
    quote = "The method uses external tools. " * 25
    item = {"kind": "entity", "candidate": {
        "id": "test", "name": "方法", "type": "Method", "summary": "摘要",
        "evidence": {"quote": quote + "Invented ending."},
    }}
    row = build_review_row(item, {}, context_mode="passage-v1", chunk_text=quote)
    assert "evidence_context" not in row
