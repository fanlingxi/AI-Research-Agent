from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.tools import pdf_tools
from app.tools.pdf_tools import _download_pdf, _infer_title, has_garbled_glyph_decoding
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


GARBLED = "孓孰孥季孩嬌季孡孬孬孹嬬 孩孴 孨孡孳"


@pytest.mark.parametrize(
    "text",
    [GARBLED, "正常中文正文。" * 200 + GARBLED, "孓孰孥\n季孩嬌季孡孬孬孹嬬"],
)
def test_garbled_glyph_detection(text):
    assert has_garbled_glyph_decoding(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Retrieval augmented generation improves accuracy by 6.91% (Zhang et al., 2024).",
        "孩子在学校学习汉字，本文研究中文检索增强生成，准确率提高了6.91%。",
        "子孔孕字存孙孝孟季孤孩孫。学 字 孩 子，宁静的家庭与学校。",
        "对 对 对实 实 实验 验 验结 结 结果 果 果",
        "孓" * 50,
        "孡孬孹",
        "孙子 孟子 孔子 季孙 孩子 孙女 孤孀",
    ],
)
def test_normal_text_is_not_garbled(text):
    assert not has_garbled_glyph_decoding(text)


def test_offset_glyphs_decode_to_ascii_and_leave_chinese_alone():
    text = "正常中文：孩子在学校。" + GARBLED + " 嬶嬮嬹嬱嬥 孯嬋孥孲"
    decoded = pdf_tools.decode_offset_glyphs(text)
    assert decoded.startswith("正常中文：孩子在学校。")
    assert "Spec" in decoded and "it has" in decoded
    assert "6.91%" in decoded and "offer" in decoded
    assert not has_garbled_glyph_decoding(decoded)


def test_only_garbled_pages_are_decoded_and_recorded(tmp_path, monkeypatch):
    path = tmp_path / "paper.pdf"
    path.touch()
    raw_pages = ["Normal English title", "", GARBLED, "正常中文正文"]
    reader = SimpleNamespace(
        pages=[SimpleNamespace(extract_text=lambda t=t: t) for t in raw_pages],
        metadata=None,
    )
    monkeypatch.setattr("pypdf.PdfReader", lambda _: reader)
    parsed = pdf_tools.parse_pdf_source(str(path))
    assert parsed.page_numbers == [1, 3, 4]
    assert parsed.page_texts[0] == raw_pages[0]
    assert parsed.page_texts[1].endswith("it has")
    assert parsed.page_texts[-1] == raw_pages[-1]
    assert parsed.metadata["glyph_offset_decoded_pages"] == [3]


def test_real_ccl_page_is_decoded():
    path = Path(
        "D:/AI-Research-Agent/data/demos/chinese-rag-20260918/papers/2024.ccl-1.3.pdf"
    )
    if not path.exists():
        pytest.skip("optional local CCL demo PDF is unavailable")
    parsed = pdf_tools.parse_pdf_source(str(path), max_pages=2)
    assert 2 in parsed.metadata["glyph_offset_decoded_pages"]
    assert not has_garbled_glyph_decoding(parsed.page_texts[1])
    assert "Specifically" in parsed.page_texts[1]
    assert "6.91" in parsed.page_texts[1]
