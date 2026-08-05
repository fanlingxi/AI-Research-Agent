from scripts.manual_acceptance import _candidate_summary, _report_quality_errors, build_parser


def test_candidate_summary_keeps_human_review_evidence() -> None:
    result = _candidate_summary(
        [
            {
                "kind": "entity",
                "candidate": {
                    "id": "candidate-1",
                    "status": "draft",
                    "type": "Method",
                    "name": "人工验收方法",
                    "confidence": 0.9,
                    "evidence": {"page_start": 3, "quote": "可人工核对的原文证据。"},
                    "merge_suggestions": [],
                },
            },
            {
                "kind": "relation",
                "candidate": {
                    "id": "candidate-2",
                    "status": "published",
                    "type": "SUPPORTS",
                    "confidence": 0.8,
                    "source_candidate_id": "candidate-1",
                    "target_candidate_id": "candidate-3",
                    "evidence": {"page_start": 4, "quote": "关系对应的原文证据。"},
                },
            },
        ]
    )

    assert result["total"] == 2
    assert result["draft"] == 1
    assert result["entities"] == 1
    assert result["relations"] == 1
    assert result["candidates"][0]["page"] == 3
    assert result["candidates"][1]["source_candidate_id"] == "candidate-1"


def test_report_quality_errors_enforces_release_thresholds() -> None:
    passing = {
        "status": "completed",
        "evaluation": {
            "evidence_grounding": 1.0,
            "citation_coverage": 0.9,
            "citation_fidelity": 1.0,
            "structure_score": 0.8,
        },
    }
    failing = {
        "status": "completed",
        "evaluation": {
            "evidence_grounding": 1.0,
            "citation_coverage": 0.5,
            "citation_fidelity": 0.5,
            "structure_score": 0.5,
        },
    }

    assert _report_quality_errors(passing) == []
    assert len(_report_quality_errors(failing)) == 3


def test_manual_parser_exposes_recovery_and_report_commands() -> None:
    parser = build_parser()

    ingestion = parser.parse_args(
        ["watch-ingestion", "ing-1", "--until", "interrupted", "--until", "completed"]
    )
    report = parser.parse_args(
        [
            "report",
            "--query",
            "测试报告证据",
            "--topic-slug",
            "manual-topic",
            "--wait",
        ]
    )

    assert ingestion.until == ["interrupted", "completed"]
    assert report.topic_slug == ["manual-topic"]
    assert report.wait is True
