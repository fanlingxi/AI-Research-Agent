import json

import pytest

from app.benchmarking.parser_trial.__main__ import main
from app.benchmarking.parser_trial.common import write
from app.benchmarking.parser_trial.zoning import (
    THRESHOLDS,
    gates,
    gold_labels,
    metrics,
    span_predictions,
    zoning_score,
)
from app.schemas.documents import DocumentBlock, ParsedDocument, TextSpan


def block(kind, start, end, *, page=1, paragraph_start=None):
    return DocumentBlock(kind=kind, text="Text deliberately differs from the raw line",
                         spans=[TextSpan(page=page, start=start, end=end)],
                         paragraph_start=paragraph_start)


def page(lines, number=1):
    return {"page": number, "lines": [
        {"id": identifier, "page": number, "start": start, "end": end, "text": text}
        for identifier, start, end, text in lines]}


def test_span_mapping_repeated_text_and_multi_page_continuation_without_matching():
    pages = [page([("a", 2, 6, "same"), ("b", 7, 11, "same")]),
             page([("c", 0, 4, "same")], 2)]
    spanning = block("paragraph", 7, 11)
    spanning.spans.append(TextSpan(page=2, start=0, end=4))
    predicted = span_predictions(pages, [block("header", 0, 6), spanning])
    assert predicted["a"]["label"] == "header"
    assert predicted["b"]["paragraph_start"] is True
    assert predicted["c"]["paragraph_start"] is False
    continued = block("paragraph", 0, 4, page=2, paragraph_start=False)
    assert span_predictions(pages[1:], [continued])["c"]["paragraph_start"] is False


@pytest.mark.parametrize("blocks", [[], [block("paragraph", 0, 3)],
                                    [block("paragraph", 0, 4), block("header", 0, 4)],
                                    [block("paragraph", 0, 2), block("paragraph", 2, 4)]])
def test_span_mapping_rejects_missing_partial_overlapping_and_split_owners(blocks):
    with pytest.raises(ValueError, match="covering block"):
        span_predictions([page([("a", 0, 4, "same")])], blocks)


def test_gold_arbitration_overrides_consensus_and_requires_all_lines():
    pages = [page([("a", 0, 4, "same"), ("b", 5, 9, "same")])]
    consensus = {"lines-test": {"lines": [{"id": "a", "label": "body", "paragraph_start": False}]}}
    resolutions = {"lines-test/line/a": {"id": "a", "label": "footer", "paragraph_start": False},
                   "lines-test/line/b": {"id": "b", "label": "body", "paragraph_start": True},
                   "images-test/X/tables/t1": {"ignored": True}}
    labels = gold_labels(pages, consensus, resolutions, "lines-test")
    assert [label["label"] for label in labels] == ["footer", "body"]
    with pytest.raises(ValueError, match="Incomplete"):
        gold_labels(pages, consensus, {}, "lines-test")
    resolutions["lines-test/line/b"]["id"] = "wrong"
    with pytest.raises(ValueError, match="changed line id"):
        gold_labels(pages, consensus, resolutions, "lines-test")


def test_metrics_all_non_body_exclusions_and_body_only_boundaries():
    truth = ["front_matter", "body", "body", "body", "header", "footer", "references"]
    predicted = ["front_matter", "other", "caption", "body", "references", "body", "footer"]
    gold = [{"id": str(i), "label": label, "page": 1, "paragraph_start": i in {1, 3}}
            for i, label in enumerate(truth)]
    predictions = {str(i): {"label": label, "paragraph_start": i in {2, 3, 5}}
                   for i, label in enumerate(predicted)}
    result = metrics(gold, predictions)
    assert result["front_matter_f1"] == 1
    assert result["margin_exclusion_recall"] == 0.5
    assert result["references_exclusion_recall"] == 1
    assert result["body_false_exclusion_rate"] == pytest.approx(2 / 3)
    assert result["paragraph_boundary_f1"] == 0.5
    assert result["scored_lines"] == 7 and result["unaligned_lines"] == 0
    predictions.pop("0")
    with pytest.raises(ValueError, match="Every gold"):
        metrics(gold, predictions)
    assert all(metrics([], {})[key] is None for key in THRESHOLDS)


def test_directional_one_percentage_point_gate_and_missing_evidence():
    old = {key: threshold for key, (_, threshold) in THRESHOLDS.items()}
    new = {**old, "margin_exclusion_recall": 0.94, "body_false_exclusion_rate": 0.015}
    result = gates(new, old)
    assert all(check["non_regression"] for check in result["checks"].values())
    assert result["status"] == "fail"  # Relative tolerance never overrides absolute gates.
    new["body_false_exclusion_rate"] = 0.0151
    new["margin_exclusion_recall"] = 0.9399
    result = gates(new, old)
    assert result["checks"]["body_false_exclusion_rate"]["non_regression"] is False
    assert result["checks"]["margin_exclusion_recall"]["non_regression"] is False
    assert gates(old, old)["status"] == "pass"
    assert gates({key: None for key in old}, old)["status"] == "incomplete"


def test_cli_offline_output_and_read_only_trial(tmp_path, monkeypatch, capsys):
    root, out = tmp_path / "trial", tmp_path / "out"
    source = tmp_path / "local.pdf"
    source.write_bytes(b"local fixture; parser injected below")
    write(root / "manifest.json", {"split": "development", "papers": [
        {"paper": "test", "language": "zh", "pdf": str(source), "page_count": 1}]})
    write(root / "prepared/test.json", [page([("a", 0, 4, "same")])])
    write(root / "consensus.json", {"lines-test": {"lines": [
        {"id": "a", "label": "body", "paragraph_start": True}]}})
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    calls = []

    def parse(source, **kwargs):
        calls.append(kwargs["parser_version"])
        return ParsedDocument(source=source, text="same", page_texts=["same"], page_numbers=[1],
                              blocks=[block("paragraph", 0, 4)])

    monkeypatch.setattr("app.benchmarking.parser_trial.zoning.parse_pdf_source", parse)
    main(["zoning-score", "--root", str(root), "--out", str(out), "--versions", "pypdf-v4"])
    report = json.loads(capsys.readouterr().out)
    assert calls == ["pypdf-v3", "pypdf-v4"]
    assert report["adopt"] is False
    assert report["arms"]["pypdf-v4"]["zh"]["metrics"]["scored_lines"] == 1
    assert report["arms"]["pypdf-v4"]["en"]["gate"]["status"] == "incomplete"
    assert "开发集已用于调参" in (out / "score.md").read_text("utf-8")
    assert all(path.read_bytes() == data for path, data in before.items())
    assert set(before) == {path for path in root.rglob("*") if path.is_file()}
    with pytest.raises(ValueError, match="read-only"):
        zoning_score(root, root / "output")
    write(root / "prepared/test.json", [page([("a", 0, 4, "DIFF")])])
    with pytest.raises(ValueError, match="raw text changed"):
        zoning_score(root, out)
