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
