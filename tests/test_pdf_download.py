import gzip
import hashlib
import io
import socket
import threading
from pathlib import Path

import httpcore
import httpx
import pytest
from pypdf import PdfWriter
from pypdf.errors import PdfReadError

from app.config.settings import Settings
from app.knowledge.service import KnowledgeIngestionService
from app.tools import pdf_download, pdf_tools


def pdf_bytes(title="Test paper"):
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_metadata({"/Title": title})
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.fixture
def network(monkeypatch):
    """Every HTTP request and DNS lookup is synthetic, including failure paths."""
    real_client = httpx.Client
    requests = []
    dns_calls = []

    def resolve(host, port, **kwargs):
        dns_calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(pdf_download, "_transport", lambda deadline: None)

    def install(handler):
        def handle(request):
            requests.append(request)
            return handler(request)

        monkeypatch.setattr(
            httpx, "Client", lambda **kwargs: real_client(
                **{**kwargs, "transport": httpx.MockTransport(handle)},
            ),
        )
        return requests, dns_calls

    # An unexpected request fails instead of accessing the network.
    install(lambda _: pytest.fail("Unexpected HTTP request"))
    return install


def options(**kwargs):
    return Settings(_env_file=None, **kwargs)


def test_redirects_validate_each_host_and_pin_ip(tmp_path, network):
    content = pdf_bytes()

    def handle(request):
        if request.headers["host"] == "first.example":
            return httpx.Response(302, headers={"location": "https://second.example/final.pdf"})
        return httpx.Response(200, content=content)

    requests, dns = network(handle)
    path = pdf_tools._download_pdf("https://first.example/paper.pdf", str(tmp_path))
    assert path.read_bytes() == content
    assert path.name.startswith(hashlib.sha256(content).hexdigest())
    assert dns == ["first.example", "second.example"]
    assert [r.url.host for r in requests] == ["93.184.216.34"] * 2
    assert [r.extensions["sni_hostname"] for r in requests] == dns
    assert [r.headers["host"] for r in requests] == dns


@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.0.0.1", "169.254.169.254", "224.0.0.1", "240.0.0.1", "0.0.0.0",
    "[::1]", "[::]", "[fe80::1]", "[fc00::1]", "[ff02::1]", "[::ffff:127.0.0.1]",
])
def test_private_targets_blocked_before_request(tmp_path, network, address):
    requests, _ = network(lambda _: pytest.fail("Private request escaped"))
    with pytest.raises(ValueError, match="prohibited"):
        pdf_tools._download_pdf(f"http://{address}/paper.pdf", str(tmp_path))
    assert requests == []
    assert list(tmp_path.iterdir()) == []


def test_redirect_to_loopback_is_blocked(tmp_path, network):
    requests, _ = network(lambda _: httpx.Response(
        302, headers={"location": "http://127.0.0.1/paper.pdf"},
    ))
    with pytest.raises(ValueError, match="prohibited"):
        pdf_tools._download_pdf("https://public.example/paper.pdf", str(tmp_path))
    assert len(requests) == 1
    assert list(tmp_path.iterdir()) == []


def test_dns_with_any_private_answer_is_blocked(tmp_path, network, monkeypatch):
    requests, _ = network(lambda _: pytest.fail("Private DNS answer escaped"))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))
        for ip in ["93.184.216.34", "192.168.1.1"]
    ])
    with pytest.raises(ValueError, match="prohibited"):
        pdf_tools._download_pdf("https://public.example/paper.pdf", str(tmp_path))
    assert not requests


@pytest.mark.parametrize("source", [
    "ftp://public.example/paper.pdf", "file://public.example/paper.pdf",
    "https://user:pass@public.example/paper.pdf", "https://public.example:8080/paper.pdf",
])
def test_invalid_url_or_port_is_rejected(tmp_path, network, source):
    with pytest.raises(ValueError):
        pdf_tools.parse_pdf_source(source, str(tmp_path))
    assert not list(tmp_path.iterdir())


def test_private_development_opt_in(tmp_path, network):
    requests, _ = network(lambda _: httpx.Response(200, content=pdf_bytes()))
    settings = options(pdf_download_allow_private_hosts=True)
    assert Settings(_env_file=None).pdf_download_allow_private_hosts is False
    path = pdf_tools._download_pdf("http://127.0.0.1:8080/paper.pdf", str(tmp_path),
                                   settings=settings)
    assert path.is_file()
    assert requests[0].url.port == 8080


def test_redirect_limit_and_relative_location(tmp_path, network):
    requests, _ = network(lambda _: httpx.Response(307, headers={"location": "/again.pdf"}))
    with pytest.raises(ValueError, match="redirect limit"):
        pdf_tools._download_pdf("https://public.example/paper.pdf", str(tmp_path),
                               settings=options(pdf_download_max_redirects=2))
    assert len(requests) == 3
    assert list(tmp_path.iterdir()) == []


class Chunks(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.read_count = 0
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            self.read_count += 1
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self):
        self.closed = True


def test_stream_limit_stops_early_and_cleans_staging(tmp_path, network):
    stream = Chunks([b"%PDF-", b"x" * 100, b"never read"])
    network(lambda _: httpx.Response(200, stream=stream))
    with pytest.raises(ValueError, match="byte limit"):
        pdf_tools._download_pdf("https://public.example/paper.pdf", str(tmp_path),
                               settings=options(pdf_download_max_bytes=10))
    assert stream.read_count == 2
    assert stream.closed
    assert list(tmp_path.iterdir()) == []


def test_decoded_bytes_are_limited(tmp_path, network):
    stream = Chunks([gzip.compress(b"%PDF-" + b"x" * 10000)])
    network(lambda _: httpx.Response(200, headers={"content-encoding": "gzip"}, stream=stream))
    with pytest.raises(ValueError, match="byte limit"):
        pdf_tools._download_pdf("https://public.example/paper.pdf", str(tmp_path),
                               settings=options(pdf_download_max_bytes=100))
    assert not list(tmp_path.iterdir())


def test_content_length_limit_before_body_read(tmp_path, network):
    stream = Chunks([b"never read"])
    network(lambda _: httpx.Response(200, headers={"content-length": "999"}, stream=stream))
    with pytest.raises(ValueError, match="byte limit"):
        pdf_tools._download_pdf("https://public.example/paper.pdf", str(tmp_path),
                               settings=options(pdf_download_max_bytes=10))
    assert stream.read_count == 0
    assert stream.closed
    assert not list(tmp_path.iterdir())


def test_total_deadline_stops_trickle_download(tmp_path, network, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(pdf_download.time, "monotonic", lambda: now[0])

    class Slow(Chunks):
        def __iter__(self):
            for chunk in super().__iter__():
                now[0] += 0.6
                yield chunk

    stream = Slow([b"%PDF-", b"data", b"never read"])
    network(lambda _: httpx.Response(200, stream=stream))
    with pytest.raises(TimeoutError, match="total time"):
        pdf_tools._download_pdf("https://public.example/paper.pdf", str(tmp_path),
                               settings=options(pdf_download_timeout_seconds=1))
    assert stream.read_count == 2
    assert stream.closed
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("slow_headers", [False, True])
def test_actual_transport_pins_host_sni_and_bounds_socket_reads(
    tmp_path, monkeypatch, slow_headers,
):
    # Exercise the HTTPX/httpcore transport itself without creating any socket.
    content = pdf_bytes()
    now = [0.0]
    operations = []
    monkeypatch.setattr(pdf_download.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
    ])

    class Wire(httpcore.NetworkStream):
        closed = False

        def read(self, max_bytes, timeout=None):
            operations.append(("read", timeout))
            now[0] += min(0.6, timeout)
            if slow_headers:
                return b"HTTP/1.1 200 OK\r\n" if len(operations) == 4 else b"X"
            return b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(content)).encode() + (
                b"\r\n\r\n" + content
            )

        def write(self, buffer, timeout=None):
            operations.append(("write", buffer))

        def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            operations.append(("tls", server_hostname))
            return self

        def close(self):
            self.closed = True

    wire = Wire()

    def connect(self, host, port, timeout=None, **kwargs):
        operations.append(("connect", host))
        return wire

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect)
    if slow_headers:
        with pytest.raises(TimeoutError, match="total time"):
            pdf_tools._download_pdf("https://public.example/paper.pdf", str(tmp_path),
                                   settings=options(pdf_download_timeout_seconds=1))
        budgets = [value for op, value in operations if op == "read"]
        assert budgets == pytest.approx([1.0, 0.4])
        assert not list(tmp_path.iterdir())
    else:
        path = pdf_tools._download_pdf("https://public.example/paper.pdf", str(tmp_path))
        assert path.read_bytes() == content
    assert ("connect", "93.184.216.34") in operations
    assert ("tls", "public.example") in operations
    assert b"Host: public.example\r\n" in next(value for op, value in operations if op == "write")
    assert wire.closed


def test_dns_wait_obeys_total_deadline(tmp_path, network, monkeypatch):
    network(lambda _: pytest.fail("DNS timeout must prevent HTTP"))
    release = threading.Event()

    def blocked_resolve(*args, **kwargs):
        release.wait(timeout=2)
        return []

    monkeypatch.setattr(socket, "getaddrinfo", blocked_resolve)
    try:
        with pytest.raises(TimeoutError, match="DNS lookup"):
            pdf_tools._download_pdf("https://public.example/paper.pdf", str(tmp_path),
                                   settings=options(pdf_download_timeout_seconds=0.1))
    finally:
        release.set()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("content", [b"not a PDF", b"%PDF-1.7 broken"])
def test_invalid_pdf_leaves_no_files(tmp_path, network, content):
    network(lambda _: httpx.Response(200, content=content))
    with pytest.raises((ValueError, PdfReadError)):
        pdf_tools.parse_pdf_source("https://public.example/paper.pdf", str(tmp_path))
    assert not list(tmp_path.iterdir())


def test_extraction_failure_leaves_no_files(tmp_path, network, monkeypatch):
    network(lambda _: httpx.Response(200, content=pdf_bytes()))

    def fail(*args, **kwargs):
        raise ValueError("extraction failed")

    monkeypatch.setattr("pypdf._page.PageObject.extract_text", fail)
    with pytest.raises(ValueError, match="extraction failed"):
        pdf_tools.parse_pdf_source("https://public.example/paper.pdf", str(tmp_path))
    assert not list(tmp_path.iterdir())


def test_transport_failure_cleans_staging(tmp_path, network):
    stream = Chunks([b"%PDF-", httpx.ReadError("disconnected")])
    network(lambda _: httpx.Response(200, stream=stream))
    with pytest.raises(httpx.ReadError):
        pdf_tools._download_pdf("https://public.example/paper.pdf", str(tmp_path))
    assert stream.closed
    assert not list(tmp_path.iterdir())


def test_same_name_imports_cannot_overwrite_registered_path(tmp_path, network):
    first_bytes = pdf_bytes("Original paper")
    second_bytes = pdf_bytes("New paper")
    network(lambda r: httpx.Response(
        200, content=first_bytes if r.headers["host"] == "first.example" else second_bytes,
    ))
    legacy = tmp_path / "paper.pdf"
    legacy.write_bytes(first_bytes)
    first = pdf_tools.parse_pdf_source("https://first.example/paper.pdf", str(tmp_path))
    registered_path = Path(first.source)
    second = pdf_tools.parse_pdf_source("https://second.example/paper.pdf", str(tmp_path))
    assert second.source != first.source
    assert registered_path.read_bytes() == first_bytes
    assert Path(second.source).read_bytes() == second_bytes
    assert legacy.read_bytes() == first_bytes
    assert first.title == "Original paper"
    assert first.metadata["file_name"] == registered_path.name
    assert first.metadata["original_source"] == "https://first.example/paper.pdf"
    again = pdf_tools.parse_pdf_source("https://first.example/paper.pdf", str(tmp_path))
    assert again.source == first.source
    renamed = pdf_tools.parse_pdf_source("https://first.example/renamed.pdf", str(tmp_path))
    assert renamed.source == first.source
    assert len(list(tmp_path.iterdir())) == 3


def test_service_passes_download_settings_and_registers_final_path(tmp_path, network, monkeypatch):
    content = pdf_bytes()
    network(lambda _: httpx.Response(200, content=content))
    monkeypatch.chdir(tmp_path)
    service = object.__new__(KnowledgeIngestionService)
    service.settings = options(pdf_download_allow_private_hosts=True,
                               pdf_download_max_bytes=len(content))
    service.parser = pdf_tools.parse_pdf_source
    source = "http://127.0.0.1:8080/paper.pdf"
    parsed = service._parse(source=source, max_pages=1)
    paper = service._paper_from_document(parsed, source)
    assert paper.metadata["local_path"] == parsed.source
    assert Path(parsed.source).read_bytes() == content
    assert Path(parsed.source).parent == (tmp_path / "data/raw_papers").resolve()
    service.settings = service.settings.model_copy(update={"pdf_download_max_bytes": 1})
    with pytest.raises(ValueError, match="byte limit"):
        service._parse(source=source, max_pages=1)
    assert Path(paper.metadata["local_path"]).read_bytes() == content
    assert list(Path(parsed.source).parent.iterdir()) == [Path(parsed.source)]


def test_corrupt_existing_hash_name_is_never_overwritten(tmp_path, network):
    content = pdf_bytes()
    network(lambda _: httpx.Response(200, content=content))
    destination = tmp_path / f"{hashlib.sha256(content).hexdigest()}-paper.pdf"
    destination.write_bytes(b"existing different content")
    with pytest.raises(ValueError, match="different content"):
        pdf_tools._download_pdf("https://public.example/paper.pdf", str(tmp_path))
    assert destination.read_bytes() == b"existing different content"
    assert list(tmp_path.iterdir()) == [destination]


@pytest.mark.parametrize("name", ["D:review.pdf", "D%3Areview.pdf", "CON.pdf", "nul", "LPT1.pdf"])
def test_windows_unsafe_names_rejected(tmp_path, network, name):
    requests, _ = network(lambda _: pytest.fail("Unsafe file name fetched"))
    with pytest.raises(ValueError, match="Unsafe PDF file name"):
        pdf_tools._download_pdf(f"https://public.example/{name}", str(tmp_path))
    assert not requests
    assert not list(tmp_path.iterdir())


def test_safe_name_and_resolved_path_stay_in_download_directory(tmp_path, network):
    network(lambda _: httpx.Response(200, content=pdf_bytes()))
    path = pdf_tools._download_pdf(
        "https://public.example/%2e%2e%5codd%20name%3F.pdf", str(tmp_path),
    )
    assert path.name.endswith("-odd-name.pdf")
    assert path.resolve().parent == tmp_path.resolve()


def test_symlink_destination_cannot_escape_directory(tmp_path, network):
    content = pdf_bytes()
    network(lambda _: httpx.Response(200, content=content))
    raw = tmp_path / "raw_papers"
    raw.mkdir()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"outside")
    target = raw / f"{hashlib.sha256(content).hexdigest()}-paper.pdf"
    try:
        target.symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks requires privileges on this Windows installation")
    with pytest.raises(ValueError, match="escapes"):
        pdf_tools._download_pdf("https://public.example/paper.pdf", str(raw))
    assert outside.read_bytes() == b"outside"
    assert list(raw.iterdir()) == [target]
