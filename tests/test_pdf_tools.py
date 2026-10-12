import httpx

from app.tools.pdf_tools import _download_pdf, _infer_title
from app.tools.text_safety import sanitize_json_value, sanitize_utf8_text


def test_download_pdf_follows_standard_redirects(tmp_path, monkeypatch) -> None:
    calls = []

    class _Response:
        content = b"%PDF-1.7 test"

        def raise_for_status(self) -> None:
            return None

    def fake_get(*args, **kwargs):
        calls.append((args, kwargs))
        return _Response()

    monkeypatch.setattr(httpx, "get", fake_get)

    path = _download_pdf(
        "https://arxiv.org/pdf/2601.14192.pdf",
        download_dir=str(tmp_path),
    )

    assert path.read_bytes() == b"%PDF-1.7 test"
    assert calls == [
        (("https://arxiv.org/pdf/2601.14192.pdf",), {"timeout": 30.0, "follow_redirects": True})
    ]


def test_text_sanitization_replaces_lone_surrogates_recursively() -> None:
    value = {"title": "数学符号 \ud835", "metadata": {"symbols": ["x\ud835y"]}}

    assert sanitize_utf8_text("x\ud835y") == "x�y"
    assert sanitize_json_value(value) == {
        "title": "数学符号 �",
        "metadata": {"symbols": ["x�y"]},
    }
    assert sanitize_utf8_text("before\x00after\nnext\tcell") == "beforeafter\nnext\tcell"


def test_title_inference_skips_attribution_boilerplate_and_stops_before_authors() -> None:
    first_page = """Provided proper attribution is provided, Google hereby grants permission to
reproduce the tables and figures in this paper solely for use in journalistic or
scholarly works.
Attention Is All You Need
Ashish Vaswani∗
Google Brain
avaswani@google.com
"""

    assert _infer_title([first_page]) == "Attention Is All You Need"


def test_fake_bold_chinese_heading_collapses_and_placeholder_title_is_ignored() -> None:
    from app.tools.pdf_tools import _infer_title, _usable_title, collapse_fake_bold

    page = collapse_fake_bold(
        "基 基 基于 于 于ChatGPT查 查 查询 询 询改 改 改写 写 写"
        "的 的 的文 文 文档 档 档检 检 检索 索 索\n"
        "李 李 李澳 澳 澳， ， ，涂 涂 涂新 新 新辉 辉 辉*\n"
        "正文 中 的 单个 重复 字 不 受 影响：哈 哈 哈"
    )
    assert page.splitlines()[0] == "基于ChatGPT查询改写的文档检索"
    assert page.splitlines()[2].endswith("哈 哈 哈")
    assert _usable_title("untitled") is None
    assert _infer_title([page]) == "基于ChatGPT查询改写的文档检索"
