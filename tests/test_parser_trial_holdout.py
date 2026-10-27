"""Holdout routing tests use only fake readers, parsers and local synthetic bytes."""

import socket
from types import SimpleNamespace

import pytest

from app.benchmarking.parser_trial.__main__ import main
from app.benchmarking.parser_trial.common import digest, read, write
from app.benchmarking.parser_trial.judge import judge_deepseek
from app.benchmarking.parser_trial.packets import packets
from app.benchmarking.parser_trial.prepare import corpus, manifest, paper_list
from app.benchmarking.parser_trial.score import score
from app.benchmarking.parser_trial.zoning import wilson, zoning_score
from app.schemas.documents import DocumentBlock, ParsedDocument, TextSpan


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbidden)


def paper(tmp_path):
    source = tmp_path / "fixture.pdf"
    source.write_bytes(b"synthetic bytes; never parsed")
    return {"paper": "new", "language": "en", "pdf": str(source), "sha256": digest(source)}


def test_explicit_list_hash_and_cli_split(tmp_path, monkeypatch):
    entry = paper(tmp_path)
    listing = tmp_path / "papers.json"
    write(listing, {"split": "holdout-d040", "papers": [entry]})
    assert paper_list(listing)["papers"] == [entry]
    calls = []
    monkeypatch.setattr("app.benchmarking.parser_trial.prepare.prepare",
                        lambda *a, **kw: calls.append((a, kw)))
    main(["prepare", "--root", str(tmp_path / "trial"), "--papers", str(listing)])
    assert calls[0][0][2] == [entry]
    assert calls[0][1] == {"split": "holdout-d040"}
    entry["sha256"] = "0" * 64
    write(listing, {"split": "holdout-d040", "papers": [entry]})
    with pytest.raises(ValueError, match="SHA-256"):
        paper_list(listing)
    monkeypatch.setattr("pypdf.PdfReader", lambda _: pytest.fail("Hash must be checked first"))
    with pytest.raises(ValueError, match="SHA-256"):
        manifest(tmp_path / "bad", [entry], split="holdout-d040")
    assert not (tmp_path / "bad/manifest.json").exists()


def test_default_manifest_bytes_unchanged(tmp_path, monkeypatch):
    entry = paper(tmp_path)
    monkeypatch.setattr("pypdf.PdfReader", lambda _: SimpleNamespace(pages=[None] * 15))
    monkeypatch.setattr("app.benchmarking.parser_trial.prepare.corpus", lambda: [entry])
    result = manifest(tmp_path / "actual")
    expected = {"schema_version": 1, "split": "development", "max_pages_per_parser": 150,
                "papers": [{**entry, "total_pages": 15, "page_count": 12}]}
    write(tmp_path / "expected.json", expected)
    assert result == expected
    assert (tmp_path / "actual/manifest.json").read_bytes() == (
        tmp_path / "expected.json").read_bytes()
    assert len(corpus()) == 12


def test_lines_only_consensus_arbitration_and_manifest_guard(tmp_path):
    write(tmp_path / "manifest.json", {"split": "holdout-d040", "papers": [
        {"paper": "new", "language": "en", "page_count": 1}]})
    write(tmp_path / "prepared/new.json", [{"page": 1, "lines": [
        {"id": "new:p001:l0001", "page": 1, "text": "text"}]}])
    assert packets(tmp_path, lines_only=True) == {"line_packets": 1, "image_packets": 0}
    assert not (tmp_path / "packets/images").exists()
    packet = read(tmp_path / "packets/lines/lines-new.json")
    assert "spans" in packet["response_format"]
    identifier = packet["lines"][0]["id"]
    left = {"packet_id": "lines-new", "lines": [
        {"id": identifier, "label": "body", "paragraph_start": True}]}
    right = {"packet_id": "lines-new", "lines": [
        {"id": identifier, "label": "header", "paragraph_start": False}]}
    write(tmp_path / "judge-deepseek/lines-new.json", left)
    write(tmp_path / "judge-qwen-text/lines-new.json", right)
    assert score(tmp_path, ["deepseek", "qwen-text"])["unresolved_items"] == 1
    write(tmp_path / "arbitration.json", {"resolutions": {
        f"lines-new/line/{identifier}": left["lines"][0]}})
    assert score(tmp_path, ["deepseek", "qwen-text"], arbitration=tmp_path / "arbitration.json")[
        "complete_labels"]
    assert read(tmp_path / "consensus.json")["lines-new"] == left
    write(tmp_path / "packets/index.json", {"line_packets": ["lines-dev"]})
    with pytest.raises(ValueError, match="manifest"):
        judge_deepseek(tmp_path, 0)


def test_synthetic_holdout_zoning_uses_own_labels(tmp_path, monkeypatch):
    root, out = tmp_path / "trial", tmp_path / "out"
    entry = paper(tmp_path)
    write(root / "manifest.json", {"split": "holdout-d040", "papers": [
        {**entry, "page_count": 1}]})
    kinds = ["front_matter", "header", "references", "paragraph"]
    labels = ["front_matter", "header", "references", "body"]
    lines = [{"id": f"new:{i}", "page": 1, "text": str(i), "start": i * 2, "end": i * 2 + 1}
             for i in range(4)]
    write(root / "prepared/new.json", [{"page": 1, "lines": lines}])
    write(root / "consensus.json", {"lines-new": {"lines": [
        {"id": line["id"], "label": label, "paragraph_start": label == "body"}
        for line, label in zip(lines, labels, strict=True)]}})
    calls = []

    def parse(source, **kwargs):
        calls.append((source, kwargs))
        return ParsedDocument(source=source, text="0 1 2 3", page_texts=["0 1 2 3"],
                              page_numbers=[1], blocks=[
            DocumentBlock(kind=kind, text=str(i), spans=[TextSpan(page=1, start=i*2, end=i*2+1)])
            for i, kind in enumerate(kinds)])

    monkeypatch.setattr("app.benchmarking.parser_trial.zoning.parse_pdf_source", parse)
    result = zoning_score(root, out)
    assert result["split"] == "holdout-d040"
    assert len(calls) == 2 and all(call[0] == entry["pdf"] for call in calls)
    arm = result["arms"]["pypdf-v4"]["en"]
    assert arm["gate"]["status"] == "pass"
    assert arm["metrics"]["wilson_95"]["body_false_exclusion_rate"][0] == 0
    assert "95% Wilson" in (out / "score.md").read_text(encoding="utf-8")
    assert wilson(0, 0) is None
    assert wilson(50, 100) == pytest.approx([0.40383153, 0.59616847])
