"""Bounded URL downloads, staged validation and immutable PDF publication."""

from __future__ import annotations

import hashlib
import ipaddress
import os
import queue
import re
import socket
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import unquote

import httpcore
import httpx

from app.config.settings import Settings, get_settings

_REDIRECTS = {301, 302, 303, 307, 308}
_RESERVED_NAME = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)", re.I)
_DNS_SLOTS = threading.BoundedSemaphore(4)


def _safe_name(url: httpx.URL) -> str:
    name = unquote(url.path).replace("\\", "/").rsplit("/", 1)[-1]
    if ":" in name or _RESERVED_NAME.match(name.rstrip(" .")):
        raise ValueError("Unsafe PDF file name")
    stem = re.sub(r"[^a-zA-Z0-9_-]+", "-", re.sub(r"\.pdf$", "", name, flags=re.I))
    return f"{stem.strip('-_')[:100] or 'downloaded-paper'}.pdf"


def _resolve_host(host: str, port: int, deadline: float):
    # OS DNS resolution has no portable timeout. Bound both caller wait time and
    # outstanding daemon lookups, including when the system resolver gets stuck.
    if not _DNS_SLOTS.acquire(timeout=_remaining(deadline)):
        raise TimeoutError("PDF DNS lookup exceeded its total time limit")
    result = queue.Queue(maxsize=1)

    def resolve():
        try:
            result.put((socket.getaddrinfo(host, port, type=socket.SOCK_STREAM), None))
        except Exception as exc:
            result.put((None, exc))
        finally:
            _DNS_SLOTS.release()

    try:
        threading.Thread(target=resolve, daemon=True).start()
    except RuntimeError:
        _DNS_SLOTS.release()
        raise
    try:
        addresses, error = result.get(timeout=_remaining(deadline))
    except queue.Empty:
        raise TimeoutError("PDF DNS lookup exceeded its total time limit") from None
    if error is not None:
        raise error
    return addresses


def _validated_target(url: httpx.URL, allow_private: bool, deadline: float) -> httpx.URL:
    if url.scheme not in {"http", "https"} or not url.host or url.userinfo:
        raise ValueError("PDF URL must use HTTP(S), a host and no credentials")
    port = url.port if url.port is not None else (443 if url.scheme == "https" else 80)
    if not allow_private and port not in {80, 443}:
        raise ValueError("PDF URL port must be 80 or 443")
    if "%" in url.host:
        raise ValueError("Scoped IP addresses are not allowed")
    try:
        addresses = [ipaddress.ip_address(url.host)]
    except ValueError:
        addresses = [
            ipaddress.ip_address(result[4][0])
            for result in _resolve_host(url.host, port, deadline)
        ]
    if not addresses:
        raise ValueError("PDF URL host has no addresses")
    for address in addresses:
        address = getattr(address, "ipv4_mapped", None) or address
        if not allow_private and (
            not address.is_global or address.is_multicast or address.is_reserved
        ):
            raise ValueError("PDF URL resolves to a prohibited address")
    # The connection uses this numeric address; Host and TLS SNI keep the URL host.
    # Disable environment proxies so they cannot resolve the original name again.
    return url.copy_with(host=str(addresses[0]))


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("PDF download exceeded its total time limit")
    return remaining


class _DeadlineStream(httpcore.NetworkStream):
    """Recompute the budget for every socket operation, including header drips."""

    def __init__(self, stream, deadline):
        self.stream = stream
        self.deadline = deadline

    def _timeout(self, timeout):
        return min(timeout or float("inf"), _remaining(self.deadline))

    def read(self, max_bytes, timeout=None):
        return self.stream.read(max_bytes, timeout=self._timeout(timeout))

    def write(self, buffer, timeout=None):
        return self.stream.write(buffer, timeout=self._timeout(timeout))

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        try:
            stream = self.stream.start_tls(
                ssl_context, server_hostname=server_hostname, timeout=self._timeout(timeout),
            )
        except BaseException:
            self.stream.close()
            raise
        return _DeadlineStream(stream, self.deadline)

    def get_extra_info(self, info):
        return self.stream.get_extra_info(info)

    def close(self):
        self.stream.close()


class _DeadlineBackend(httpcore.SyncBackend):
    def __init__(self, deadline):
        self.deadline = deadline

    def connect_tcp(self, host, port, timeout=None, **kwargs):
        stream = super().connect_tcp(
            host, port, timeout=min(timeout or float("inf"), _remaining(self.deadline)), **kwargs,
        )
        return _DeadlineStream(stream, self.deadline)


def _transport(deadline: float) -> httpx.HTTPTransport:
    transport = httpx.HTTPTransport(trust_env=False, retries=0)
    # HTTPX does not expose network_backend. Keep this compatibility seam here;
    # regression tests exercise the real HTTPX/httpcore path with synthetic I/O.
    transport._pool._network_backend = _DeadlineBackend(deadline)
    return transport


def _contained(directory: Path, name: str) -> Path:
    path = directory / name
    if not path.resolve().is_relative_to(directory):
        raise ValueError("PDF path escapes the download directory")
    return path


def _publish(staged: Path, directory: Path, name: str, digest: str) -> Path:
    existing = next(directory.glob(f"{digest}-*.pdf"), None)
    target = _contained(directory, existing.name if existing else f"{digest}-{name}")
    try:
        # Hard-link publication is atomic and fails if the destination exists.
        # Unlike replace()/rename() on POSIX, it can never overwrite another file.
        os.link(staged, target)
    except FileExistsError:
        # Do not trust a file just because its name contains the expected hash.
        if target.is_symlink() or not target.is_file():
            raise ValueError("Existing PDF destination is not a regular file") from None
        with target.open("rb") as existing:
            actual = hashlib.file_digest(existing, "sha256").hexdigest()
        if actual != digest:
            raise ValueError("Existing PDF destination has different content") from None
    return target


def download_pdf(
    source: str, download_dir: str, *, validate: Callable[[Path], None],
    settings: Settings | None = None,
) -> Path:
    settings = settings or get_settings()
    deadline = time.monotonic() + settings.pdf_download_timeout_seconds
    url = httpx.URL(source)
    name = _safe_name(url)
    directory = Path(download_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    staged = None
    try:
        for hop in range(settings.pdf_download_max_redirects + 1):
            _remaining(deadline)
            target_url = _validated_target(
                url, settings.pdf_download_allow_private_hosts, deadline,
            )
            # A fresh pool per hop also forces TLS certificate verification for
            # the current hostname when different sites share a validated IP.
            with httpx.Client(
                follow_redirects=False, trust_env=False, transport=_transport(deadline),
            ) as client:
                timeout = min(30.0, _remaining(deadline))
                host = url.netloc.decode("ascii")
                with client.stream(
                    "GET", target_url, headers={"Host": host, "Accept-Encoding": "identity"},
                    extensions={"sni_hostname": url.host}, timeout=timeout,
                ) as response:
                    _remaining(deadline)
                    if response.status_code in _REDIRECTS:
                        if hop == settings.pdf_download_max_redirects:
                            raise ValueError("PDF URL exceeded its redirect limit")
                        location = response.headers.get("location")
                        if not location:
                            raise ValueError("PDF redirect has no location")
                        url = url.join(location)
                        continue
                    response.raise_for_status()
                    length = response.headers.get("content-length")
                    if length is not None and int(length) > settings.pdf_download_max_bytes:
                        raise ValueError("PDF download exceeds its byte limit")
                    digest = hashlib.sha256()
                    size = 0
                    with tempfile.NamedTemporaryFile(
                        dir=directory, prefix=".pdf-download-", suffix=".pdf", delete=False,
                    ) as output:
                        staged = Path(output.name)
                        for chunk in response.iter_bytes():
                            _remaining(deadline)
                            size += len(chunk)
                            if size > settings.pdf_download_max_bytes:
                                raise ValueError("PDF download exceeds its byte limit")
                            digest.update(chunk)
                            output.write(chunk)
                        _remaining(deadline)
                    break
        with staged.open("rb") as downloaded:
            if downloaded.read(5) != b"%PDF-":
                raise ValueError("Downloaded content is not a PDF")
        validate(staged)
        _remaining(deadline)
        return _publish(staged, directory, name, digest.hexdigest())
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
