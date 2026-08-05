import httpx

from app.tools.pdf_tools import _download_pdf
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
