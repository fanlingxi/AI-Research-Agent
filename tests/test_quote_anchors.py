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
