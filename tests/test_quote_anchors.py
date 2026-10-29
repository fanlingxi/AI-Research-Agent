import pytest

from app.retrieval.anchors import contains_quote, locate_quote


def test_locates_quote_across_layout_noise_and_returns_exact_span():
    text = "Dense passage\nretrieval uses “dual‐encoders” trained in-batch."
    quote = 'passage retrieval uses "dual-encoders"'
    start, end = locate_quote(quote, text)
    assert text[start:end] == "passage\nretrieval uses “dual‐encoders”"


def test_cjk_spacing_and_fullwidth_forms_match():
    assert contains_quote("检索增强生成（RAG）能够减少幻觉", "检索 增强\n生成(RAG)能够减少幻觉。")


def test_paraphrase_ellipsis_and_reordering_still_fail():
    text = "The retriever is trained with in-batch negatives and BM25 hard negatives."
    assert not contains_quote("The retriever is trained ... BM25 hard negatives", text)
    assert not contains_quote("The retriever uses in-batch negatives", text)
    assert not contains_quote("", text)


def test_line_break_hyphen_may_be_dropped_or_kept():
    text = "sparse retrieval. Be- sides, it handles pseudo- documents of up to 8,192 to- kens."
    quote = "Besides, it handles pseudo-documents of up to 8,192 to- kens."
    start, end = locate_quote(quote, text)
    assert text[start:end] == "Be- sides, it handles pseudo- documents of up to 8,192 to- kens."
    assert contains_quote("it handles pseudodocuments", text)
    assert contains_quote("Be- sides, it handles", text)


def test_line_break_hyphen_tolerance_stays_narrow():
    text = "Scores rose 10- 20 points, State- Of the art - see Table 2. Well-known results."
    assert not contains_quote("rose 1020 points", text)
    assert not contains_quote("StateOf the art", text)
    assert not contains_quote("art see Table", text)
    assert not contains_quote("Wellknown results", text)
    assert not contains_quote("Besides, it", "Be- sides it")


@pytest.mark.parametrize("quote,text,accepted", [
    ("RAG(A)", "ＲＡＧ（Ａ）", True),
    ("。「A」、・", "｡｢A｣､･", True),
    ('"dual-encoders"', '“dual—encoders”', True),
    ("检索增强生成", "检索 增强\n生成", True),
    ("retrieval", "re\u00adtrie\u200bval\ufeff", True),
    ("Besides pseudo-documents", "Be- sides pseudo-\ndocuments", True),
    ("ff fi fl ffi ffl st st", "ﬀ ﬁ ﬂ ﬃ ﬄ ﬅ ﬆ", True),
    ("x2", "x²", False),
    ("CO2", "CO₂", False),
    ("10 mg", "10 Mg", False),
    ("IV", "Ⅳ", False),
    ("1⁄2", "½", False),
    ("1", "①", False),
    ("alpha ... gamma", "alpha beta gamma", False),
    ("gamma beta alpha", "alpha beta gamma", False),
    ("The retriever uses negatives", "The retriever is trained with negatives", False),
])
def test_typographic_v2_synthetic_boundaries(quote, text, accepted):
    span = locate_quote(quote, text, policy="typographic-v2")
    assert contains_quote(quote, text, policy="typographic-v2") is accepted
    assert (span is not None) is accepted
    if accepted:
        assert text[slice(*span)] == text.rstrip("\ufeff")


@pytest.mark.parametrize("symbol,replacement", [
    ("′", "'"), ("″", '"'), ("−", "-"), ("\u200c", ""), ("\u200d", ""),
])
def test_typographic_v2_preserves_scientific_and_joining_symbols(symbol, replacement):
    text = f"A{symbol}B"
    assert contains_quote(text, text, policy="typographic-v2")
    assert not contains_quote(f"A{replacement}B", text, policy="typographic-v2")


@pytest.mark.parametrize("quote,text", [
    ("x2", "x²"), ("CO2", "CO₂"), ("10 mg", "10 Mg"), ("IV", "Ⅳ"),
    ("1⁄2", "½"), ("1", "①"), ("A'B", "A′B"), ("A''B", "A″B"),
    ("A-B", "A−B"), ("AB", "A\u200cB"), ("AB", "A\u200dB"),
])
def test_legacy_default_keeps_compatibility_folding(quote, text):
    assert contains_quote(quote, text)
    assert locate_quote(quote, text) == locate_quote(quote, text, policy="legacy-v1")


def test_typographic_v2_offsets_and_empty_or_unknown_policy():
    text = "prefix “Ａﬃnity” suffix"
    start, end = locate_quote('"Affinity"', text, policy="typographic-v2")
    assert text[start:end] == "“Ａﬃnity”"
    for quote in ("", " \u00ad\u200b\ufeff"):
        assert not contains_quote(quote, text, policy="typographic-v2")
    for quote in ("", "Affinity"):
        with pytest.raises(ValueError, match="Unknown anchor policy"):
            locate_quote(quote, text, policy="typo")
