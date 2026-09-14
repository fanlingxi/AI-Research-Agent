from types import SimpleNamespace

import pytest

from app.benchmarking.reading_trial import (
    InlineReadingWorkflow,
    ReadingPlugin,
    RecordedClient,
    run,
)
from app.domain_plugins.errors import DomainPluginConflictError
from app.domain_plugins.registry import DomainPluginRegistry


class Delegate:
    provider_name = "fake"
    model = "fake"
    last_usage_complete = True
    last_usage = {"input_tokens": 12, "output_tokens": 8}

    def invoke(self, prompt, system_prompt=None):
        return "result"


def test_only_identical_analysis_prompts_replay_and_drafts_still_call(tmp_path):
    cache = {}
    baseline = RecordedClient(Delegate(), tmp_path / "original", cache)
    assert baseline.invoke("analyze task", "system") == "result"
    compact = RecordedClient(Delegate(), tmp_path / "compact", cache)
    compact.invoke("analyze task", "system")
    assert compact.paid == 0 and compact.last_usage == {"input_tokens": 0, "output_tokens": 0}
    compact.invoke("analyze task changed", "system")
    compact.invoke("Research input: same", "system")
    compact.invoke("Research input: same", "system")
    assert compact.paid == 3
    compact.invoke("another prompt", "system")
    with pytest.raises(ValueError, match="limit"):
        compact.invoke("too many", "system")


def test_timeout_does_not_inherit_previous_usage(tmp_path):
    delegate = Delegate()
    client = RecordedClient(delegate, tmp_path, {})
    client.invoke("Research input: success")

    def fail(*args):
        raise TimeoutError("details")

    delegate.invoke = fail
    with pytest.raises(TimeoutError):
        client.invoke("Research input: failure")
    assert client.last_usage == {} and client.calls[-1]["usage"] is None
    assert client.paid == 2 and client.calls[-1]["status"] == "failed"


def test_experiment_pins_have_separate_versions_and_checkpoint_namespaces():
    old, compact = ReadingPlugin(False), ReadingPlugin(True)
    assert old.manifest.version != compact.manifest.version
    assert (
        old.workflow_spec("research").checkpoint_namespace
        != compact.workflow_spec("research").checkpoint_namespace
    )
    old_pin = DomainPluginRegistry([old]).pin("research", "research")
    with pytest.raises(DomainPluginConflictError, match="unavailable"):
        DomainPluginRegistry([compact]).resolve_pin(old_pin)
    assert (
        old.build_workflow(SimpleNamespace(llm=None), old_pin).__class__.__name__
        == "ResearchWorkflow"
    )


def test_live_reading_requires_explicit_entry_outside_offline_tests(offline_trial_inputs):
    offline_trial_inputs("reading_trial")
    assert run()["max_model_calls"] == 24
    with pytest.raises(ValueError, match="disabled"):
        run(execute=True)


def test_inline_view_keeps_quote_beside_evidence_and_removes_only_management(monkeypatch):
    original = {
        "task": {"goal": "question"},
        "knowledge": {
            "claim_bundles": [
                {
                    "claim": {"claim_id": "c1", "statement": "NOT applicable " * 20},
                    "evidence": [
                        {"evidence_id": "e1", "chunk_id": "k1", "quote": "NOT applicable " * 20}
                    ],
                    "selection": {"reason": "ranking only"},
                }
            ]
        },
    }
    monkeypatch.setattr("app.agent.research_workflow.research_input_payload", lambda p: original)
    view = InlineReadingWorkflow(SimpleNamespace(llm=None))._research_input(None)
    evidence = view["knowledge"]["claim_bundles"][0]["evidence"][0]
    assert evidence == original["knowledge"]["claim_bundles"][0]["evidence"][0]
    assert isinstance(evidence["quote"], str)
    assert "selection" not in view["knowledge"]["claim_bundles"][0]
    assert "selection" in original["knowledge"]["claim_bundles"][0]
