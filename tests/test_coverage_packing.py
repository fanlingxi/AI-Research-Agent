"""Offline integration and exact-budget regressions for optional coverage-v3."""

import hashlib
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.agent.models import AgentRunCreateRequest
from app.context import service as context_service
from app.context.coverage_packing import pack_coverage
from app.context.models import (
    ContextBuildRequest,
    KnowledgeContext,
    canonical_package_sha256,
    runtime_context_payload,
    runtime_context_token_count,
)
from app.context.service import ContextBuilderService
from app.knowledge.schemas import CandidateEntity, EvidenceSpan
from app.research_profiles import STANDARD
from app.retrieval.coverage import CoreCoverageReranker, coverage_indices
from app.retrieval.reranking import input_digest
from app.schemas.documents import DocumentChunk
from tests.test_compact_reading import _fixed_package, _shared_source_setup
from tests.test_context_neighbors import _setup
from tests.test_selection_retry import FakeClock, ScriptedSelection


class Anchors:
    def __init__(self, parts):
        self.parts = parts
        self.calls = 0

    def rank(self, payload):
        self.calls += 1
        parts = []
        for text, markers in self.parts:
            anchors = []
            for marker in markers:
                candidate = next(c for c in payload['candidates']
                                 if marker in c['passages'][0])
                anchors.append({'index': candidate['index'],
                                'quote': candidate['passages'][0][:40]})
            parts.append({'question_part': text, 'anchors': anchors})
        return {'status': 'ranked', 'parts': parts, 'model_calls': 1,
                'indices': coverage_indices(payload, parts), 'input_sha256': input_digest(payload)}


def _question(repo, task, text):
    with repo.database.connect() as db:
        db.execute('UPDATE workspace_tasks SET goal=? WHERE id=?', (text, task.id))


def _ids(package):
    return [b.claim.claim_id for b in package.knowledge.claim_bundles]


def _audit(package):
    return package.retrieval_audit.parameters['coverage_packing']


def _build(builder, task, strategy='coverage-v3', fmt='inline-v1', budget=6000, **kwargs):
    return builder.preview_context(ContextBuildRequest(
        task_id=task.id, evidence_reranking=strategy, reading_format=fmt,
        max_tokens=budget, snapshot_id='fixed-snapshot', **kwargs,
    ))


@pytest.mark.parametrize('fmt', ['legacy', 'research-v1', 'inline-v1', 'compact-v1'])
def test_large_first_anchor_is_skipped_and_later_parts_survive(tmp_path, fmt):
    repo, task, claims = _setup(tmp_path, parts=[
        'Needle oversized support. ' * 200,
        'Needle small second support. ' * 4,
        'Needle small third support. ' * 4,
    ], publish_last=True)
    with repo.database.connect() as db:
        db.execute('UPDATE claims SET statement=? WHERE id=?',
                   ('Needle oversized synthesis. ' * 3000, claims[0]))
    _question(repo, task, 'Needle first; Needle second; Needle third; Needle missing')
    ranker = Anchors([
        ('Needle first', ['oversized']), ('Needle second', ['second support']),
        ('Needle third', ['third support']), ('Needle missing', []),
    ])
    builder = ContextBuilderService(repo, coverage_reranker=ranker)
    old = _build(builder, task, 'coverage-v2', fmt, budget=12000)
    new = _build(builder, task, fmt=fmt, budget=12000)
    assert _ids(old) == []
    assert _ids(new) == claims[1:]
    assert runtime_context_token_count(new) == new.token_usage.used <= 12000
    audit = _audit(new)
    assert [p['selected'] for p in audit['parts']] == [False, True, True, False]
    assert audit['parts'][0]['anchors'][0]['reason'] == 'over_budget'
    assert audit['parts'][3]['reason'] == 'not_anchored'
    assert audit['first_fit_skipped_bundles'] == 1
    assert ranker.calls == 2
    assert 'support' not in json.dumps(audit) and 'Needle' not in json.dumps(audit)
    print(f'{fmt}: coverage-v2={len(_ids(old))}, coverage-v3={len(_ids(new))}; '
          f'tokens={new.token_usage.used}/12000')


def _synthetic(fmt='inline-v1'):
    base = _fixed_package(fmt)
    template = base.knowledge.claim_bundles[0]
    bundles = []
    for index in range(4):
        bundle = template.model_copy(deep=True)
        bundle.claim.claim_id = f'claim-{index}'
        bundle.evidence[0].claim_id = f'claim-{index}'
        bundle.evidence[0].evidence_id = f'evidence-{index}'
        bundles.append(bundle)
    base.knowledge = KnowledgeContext()
    base.constraints.max_context_tokens = 16000
    record = {
        'candidate_claim_ids': [b.claim.claim_id for b in bundles],
        'original_ranks': {b.claim.claim_id: i for i, b in enumerate(bundles)},
        'parts': [],
    }
    return base, bundles, record


def _part(text, indices, bundles):
    return {'question_part': text, 'anchors': [
        {'index': index, 'quote': bundles[index].chunks[0].content[:40]} for index in indices
    ]}


def _pack(base, bundles, record, titles=None, groups=None):
    selected, audit = pack_coverage(base, bundles, record, titles or {}, groups)
    package = base.model_copy(update={'knowledge': KnowledgeContext(claim_bundles=selected)})
    assert runtime_context_token_count(package) == audit['measured_tokens']
    assert audit['measured_tokens'] <= base.constraints.max_context_tokens
    return _ids(package), audit


def _budget_for(base, bundles):
    return runtime_context_token_count(base.model_copy(update={
        'knowledge': KnowledgeContext(claim_bundles=bundles),
    }))


def test_breadth_before_second_anchor_and_insufficient_budget_keeps_part_order():
    base, bundles, record = _synthetic()
    record['parts'] = [_part('first question', [0, 1], bundles),
                       _part('second question', [2], bundles)]
    base.constraints.max_context_tokens = _budget_for(base, [bundles[0], bundles[2]])
    ids, audit = _pack(base, bundles, record)
    assert ids == ['claim-0', 'claim-2']
    assert all(p['selected'] for p in audit['parts'])
    base.constraints.max_context_tokens = _budget_for(base, [bundles[0]])
    ids, audit = _pack(base, bundles, record)
    assert ids == ['claim-0']
    assert audit['parts'][1]['reason'] == 'over_budget'


def _two_sources(bundles):
    for bundle in bundles:
        for source in bundle.sources:
            source.title = 'Alpha Method'
        for document in bundle.documents:
            document.title = 'Alpha Method'
    other = bundles[2]
    other.sources[0].source_id = 'source-beta'
    other.sources[0].title = 'Beta Method'
    other.documents[0].source_id = 'source-beta'
    other.documents[0].document_id = 'document-beta'
    other.documents[0].title = 'Beta Method'
    other.chunks[0].document_id = 'document-beta'
    other.chunks[0].chunk_id = 'chunk-beta'
    other.evidence[0].source_id = 'source-beta'
    other.evidence[0].chunk_id = 'chunk-beta'
    return {bundles[0].sources[0].source_id: {'Alpha Method'}, 'source-beta': {'Beta Method'}}


@pytest.mark.parametrize('fmt', ['inline-v1', 'compact-v1'])
@pytest.mark.parametrize('question', [
    'Compare Alpha Method versus Beta Method', '比较Alpha Method和Beta Method',
    'Describe Alpha Method and Beta Method separately',
])
def test_comparison_constraint_and_soft_source_preference(fmt, question):
    base, bundles, record = _synthetic(fmt)
    titles = _two_sources(bundles)
    record['parts'] = [_part(question, [0, 1, 2], bundles)]
    base.constraints.max_context_tokens = _budget_for(base, [bundles[0], bundles[2]])
    ids, audit = _pack(base, bundles, record, titles)
    assert ids == ['claim-0', 'claim-2']
    part = audit['parts'][0]
    assert part['source_constraint_triggered']
    assert part['matched_source_ids'] == list(titles)
    assert all(source['selected'] for source in part['sources'])
    # Non-comparison still prefers diverse anchors, without a hard source requirement.
    record['parts'][0]['question_part'] = 'Explain the method'
    ids, audit = _pack(base, bundles, record, titles)
    assert ids == ['claim-0', 'claim-2']
    assert not audit['parts'][0]['source_constraint_triggered']


@pytest.mark.parametrize('question,extra_titles', [
    ('Explain Alpha Method and Beta Method', {}),
    ('Compare Alpha versus Beta', {}),
    ('Compare these two methods', {}),
    ('Do not compare Alpha Method and Beta Method', {}),
    ('Compare Alpha Method and Beta Method', {'denied-lookalike': {'Alpha Method'}}),
    ('Compare Alpha Method and Beta Method', {'overlap': {'Method and Beta Method'}}),
])
def test_ambiguous_or_noncomparison_spans_do_not_require_sources(question, extra_titles):
    base, bundles, record = _synthetic()
    titles = _two_sources(bundles)
    titles.update(extra_titles)
    record['parts'] = [_part(question, [0, 2], bundles)]
    _, audit = _pack(base, bundles, record, titles)
    assert not audit['parts'][0]['source_constraint_triggered']
    assert audit['parts'][0]['matched_source_ids'] == []


@pytest.mark.parametrize('anchored', [False, True])
def test_source_constraint_reports_missing_anchor_or_budget(anchored):
    base, bundles, record = _synthetic()
    titles = _two_sources(bundles)
    record['parts'] = [_part('分别说明 Alpha Method 与 Beta Method',
                            [0, 2] if anchored else [0], bundles)]
    base.constraints.max_context_tokens = _budget_for(base, [bundles[0]])
    _, audit = _pack(base, bundles, record, titles)
    assert audit['parts'][0]['sources'][1] == {
        'source_id': 'source-beta', 'selected': False,
        'reason': 'over_budget' if anchored else 'not_anchored',
    }


@pytest.mark.parametrize('budget', [6000, 16000])
def test_compact_builder_packs_more_shared_source_bundles(tmp_path, budget):
    repo, task = _shared_source_setup(tmp_path)
    builder = ContextBuilderService(repo, coverage_reranker=Anchors([('Needle', ['Needle'])]))
    packages = [_build(builder, task, fmt=fmt, budget=budget)
                for fmt in ('inline-v1', 'compact-v1')]
    assert len(_ids(packages[1])) > len(_ids(packages[0]))
    for package in packages:
        assert runtime_context_token_count(package) == _audit(package)['measured_tokens'] <= budget
    print(f'v3 shared source budget={budget}: inline={len(_ids(packages[0]))}, '
          f'compact={len(_ids(packages[1]))}')


def test_groups_are_atomic_and_keep_dependencies_when_prefix_is_skipped():
    base, bundles, record = _synthetic('compact-v1')
    bundles[0].claim.statement = 'large ' * 10000
    groups = [
        {'claim_ids': ['claim-0', 'claim-1'], 'context_claim_ids': ['claim-0', 'claim-1']},
        {'claim_ids': ['claim-2'], 'context_claim_ids': ['claim-1', 'claim-2']},
    ]
    record['parts'] = [_part('first', [0], bundles), _part('second', [2], bundles),
                       _part('third', [3], bundles)]
    base.constraints.max_context_tokens = _budget_for(base, [bundles[3]])
    ids, audit = _pack(base, bundles, record, groups=groups)
    assert ids == ['claim-3']
    assert audit['first_fit_skipped_bundles'] == 3
    base.constraints.max_context_tokens = _budget_for(base, bundles)
    ids, _ = _pack(base, bundles, record, groups=groups)
    assert set(ids) == {f'claim-{i}' for i in range(4)}


def test_fill_uses_original_similarity_order_and_each_bundle_is_serialized_once(monkeypatch):
    from app.context import coverage_packing

    base, bundles, record = _synthetic('compact-v1')
    record['parts'] = [_part('first', [2], bundles)]
    record['original_ranks'] = {'claim-3': 0, 'claim-1': 1, 'claim-0': 2, 'claim-2': 3}
    actual = coverage_packing.runtime_context_payload
    sizes = []

    def counted(package):
        sizes.append(len(package.knowledge.claim_bundles))
        return actual(package)

    monkeypatch.setattr(coverage_packing, 'runtime_context_payload', counted)
    ids, _ = _pack(base, bundles, record)
    assert ids == ['claim-2', 'claim-3', 'claim-1', 'claim-0']
    assert sizes == [0, 1, 1, 1, 1]


@pytest.mark.parametrize('failure', ['transient', 'structure', 'unanchored'])
@pytest.mark.parametrize('fmt', ['inline-v1', 'compact-v1'])
def test_fallback_uses_identical_v2_reading_and_retry_policy(tmp_path, monkeypatch, failure, fmt):
    monkeypatch.setattr(context_service, '_now', lambda: '2026-01-01T00:00:00+00:00')
    repo, task, _ = _setup(tmp_path, parts=['Needle evidence. ' * 20] * 3, publish_last=True)
    packages, records = [], []
    for strategy in ('coverage-v2', 'coverage-v3'):
        clock = FakeClock()
        steps = {
            'transient': [ConnectionError(), TimeoutError()],
            'structure': ['{"parts":42}'],
            'unanchored': [json.dumps({'parts': [{'question_part': 'Needle', 'anchors': []}]})],
        }[failure]
        llm = ScriptedSelection(steps, clock)
        builder = ContextBuilderService(repo, coverage_reranker=CoreCoverageReranker(
            llm, sleep=clock.sleep, clock=clock,
        ))
        package = _build(builder, task, strategy, fmt, budget=4000)
        packages.append(package)
        records.append(package.retrieval_audit.parameters['reranking'])
        assert llm.calls == (2 if failure == 'transient' else 1)
    assert _ids(packages[0]) == _ids(packages[1])
    assert packages[0].diagnostics == packages[1].diagnostics
    # Strategy changes the audit hash, which is intentionally part of reading input.
    payloads = [runtime_context_payload(p.model_copy(update={'package_sha256': '0' * 64}))
                for p in packages]
    assert payloads[0] == payloads[1]
    for field in ('attempts', 'model_calls', 'retry_policy', 'reason', 'input_sha256'):
        assert records[0][field] == records[1][field]
    assert _audit(packages[1])['status'] == 'fallback'


def test_requests_accept_v3_and_preserve_standard_and_frozen_snapshots():
    request = ContextBuildRequest(task_id='task', evidence_reranking='coverage-v3')
    assert request.evidence_reranking == 'coverage-v3'
    assert AgentRunCreateRequest(evidence_reranking='coverage-v3').evidence_reranking == (
        'coverage-v3'
    )
    assert STANDARD.context_request(ContextBuildRequest(task_id='task')).evidence_reranking == (
        'coverage-v2'
    )
    with pytest.raises(ValidationError, match='cannot be reranked'):
        AgentRunCreateRequest(context_snapshot_id='old', evidence_reranking='coverage-v3')


def test_v3_reuses_exact_v2_prompt_retry_and_persists_without_changing_old_snapshot(
    tmp_path, monkeypatch,
):
    repo, task, _ = _setup(tmp_path)
    prompts, packages = [], []

    def response(prompt):
        prompts.append(prompt)
        payload, _ = json.JSONDecoder().raw_decode(prompt[prompt.index('{"candidates"'):])
        return json.dumps({'parts': [{'question_part': 'Needle', 'anchors': [{
            'index': 0, 'quote': payload['candidates'][0]['passages'][0][:40],
        }]}]})

    builder = ContextBuilderService(repo)
    for strategy in ('coverage-v2', 'coverage-v3'):
        clock = FakeClock()
        llm = ScriptedSelection([ConnectionError(), response], clock)
        selector = CoreCoverageReranker(llm, sleep=clock.sleep, clock=clock)
        # Exercise the default factory, not just the adapter injection path.
        monkeypatch.setattr(context_service, 'CoreCoverageReranker', lambda s=selector: s)
        package = builder.build_context(ContextBuildRequest(
            task_id=task.id, max_tokens=16000, evidence_reranking=strategy,
            reading_format='compact-v1',
        ))
        packages.append(package)
        audit = package.retrieval_audit.parameters['reranking']
        assert audit['strategy'] == strategy
        assert audit['model_calls'] == llm.calls == 2
        assert audit['retry_policy'] == 'selection-call-v1'
        assert clock.sleeps == [2]
    assert prompts[0] == prompts[1]
    for package in packages:
        restored = builder.snapshot_repository.get(package.snapshot_id)
        assert restored == package
        assert canonical_package_sha256(restored) == package.package_sha256


@pytest.mark.parametrize('allowed', [True, False])
def test_builder_matches_only_sqlite_allowed_source_titles(tmp_path, allowed):
    repo, task, _ = _setup(tmp_path, parts=['Needle Alpha evidence. ' * 5], publish_last=True)
    with repo.database.connect() as db:
        db.execute("UPDATE sources SET title='Alpha Method'")
        db.execute("UPDATE documents SET title='Alpha Method'")
        ingestion_id = db.execute('SELECT id FROM ingestions LIMIT 1').fetchone()[0]
    scopes = repo.memory_repository.list_project_knowledge_scopes(task.project_id)
    scope = scopes[0].collection_slug if allowed else repo.create_collection('Outside').slug
    text = 'Needle Beta supporting evidence. ' * 5
    repo.add_document(ingestion_id=ingestion_id, document_id='beta-paper', title='Beta Method',
                      source='pdf', source_url=None, local_path='beta.pdf', pages=1, metadata={})
    repo.core_repository.record_source_document(
        document_id='beta-paper', title='Beta Method', uri='beta.pdf', content=text,
        parser_version='fixture-v1', metadata={'source_version': 'v1'},
    )
    repo.core_repository.upsert_chunks([DocumentChunk(
        id='beta-chunk', paper_id='beta-paper', title='Beta Method', text=text, chunk_index=0,
        token_count=50, source_tier='primary_fulltext', metadata={'page_start': 1, 'page_end': 1},
    )])
    candidate = CandidateEntity(
        id='beta-candidate', ingestion_id=ingestion_id, topic_slug=scope, name='Needle Beta',
        type='Paper', summary=text, confidence=1,
        evidence=EvidenceSpan(paper_id='beta-paper', chunk_id='beta-chunk', quote=text,
                              page_start=1, page_end=1),
    )
    repo.add_candidate_entity(candidate)
    repo.publish_entity(candidate.id, review_note='fixture')
    question = 'Compare Alpha Method and Beta Method'
    _question(repo, task, question)
    ranker = Anchors([(question, ['Alpha evidence', 'Beta supporting'] if allowed
                        else ['Alpha evidence'])])
    builder = ContextBuilderService(repo, coverage_reranker=ranker)
    package = _build(builder, task, fmt='compact-v1')
    part = _audit(package)['parts'][0]
    assert part['source_constraint_triggered'] is allowed
    assert len(part['matched_source_ids']) == (2 if allowed else 0)
    assert len({s.source_id for b in package.knowledge.claim_bundles for s in b.sources}) == (
        2 if allowed else 1
    )


def _frozen_assembly(monkeypatch, strategy, fmt):
    """Fixed inputs exercise the actual old tail-trimming loop and envelope bytes."""
    base, bundles, _ = _synthetic(fmt)
    bundles[0].claim.statement = 'Oversized first bundle. ' * 500
    for name in ('project', 'task', 'memory', 'artifacts'):
        value = getattr(base, name)
        helper = '_artifact_context' if name == 'artifacts' else f'_{name}_context'
        monkeypatch.setattr(context_service, helper, lambda *args, v=value: v)
    monkeypatch.setattr(context_service, '_knowledge_bundle', lambda item: item)
    monkeypatch.setattr(context_service, '_now', lambda: '2026-01-01T00:00:00+00:00')
    memory = SimpleNamespace(
        project=SimpleNamespace(id=base.project.project_id, revision=1, domain='test', metadata={}),
        knowledge_scopes=[], artifacts=[],
    )
    task = SimpleNamespace(id=base.task.task_id, revision=1)
    request = ContextBuildRequest(task_id=task.id, snapshot_id='fixed-assembly',
                                  max_tokens=6000, reading_format=fmt, evidence_reranking=strategy)
    builder = object.__new__(ContextBuilderService)
    return builder._assemble_package(
        request, memory_snapshot=memory, task=task, ranked_bundles=bundles,
        coverage='available', vector_candidates=0, graph_candidates=0, verified_bundles=4,
        notices=[],
    )[0]


@pytest.mark.parametrize('strategy', ['coverage-v1', 'coverage-v2'])
@pytest.mark.parametrize('fmt', ['legacy', 'research-v1', 'inline-v1', 'compact-v1'])
def test_old_coverage_assembly_bytes_and_hashes_match_pre_v3(monkeypatch, strategy, fmt):
    from pathlib import Path

    expected = json.loads((Path(__file__).parent / 'fixtures' / 'coverage_packing_baselines.json')
                          .read_text('utf-8'))[f'{strategy}/{fmt}']
    package = _frozen_assembly(monkeypatch, strategy, fmt)
    encoded = json.dumps(package.model_dump(mode='json'), ensure_ascii=False,
                         sort_keys=True, separators=(',', ':'))
    assert hashlib.sha256(encoded.encode()).hexdigest() == expected['package_bytes_sha256']
    assert canonical_package_sha256(package) == expected['package_sha256']
    assert package.token_usage.used == expected['tokens']
    assert len(_ids(package)) == expected['bundles']
