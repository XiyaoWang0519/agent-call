"""A private, pinned cloudflared helper for temporary local HTTPS access."""

from __future__ import annotations

import hashlib
import io
import os
import platform
import re
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import IO, Self

CLOUDFLARED_VERSION = "2026.8.3"
DOWNLOAD_TIMEOUT_SECONDS = 30
DOWNLOAD_DEADLINE_SECONDS = 120
STARTUP_TIMEOUT_SECONDS = 60
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
STOP_TIMEOUT_SECONDS = 5


class TunnelError(RuntimeError):
    """A value-free failure suitable for an operator-facing message."""


@dataclass(frozen=True)
class _Asset:
    name: str
    sha256: str


# GitHub asset digests for this exact official release, not its release-body
# table (the Darwin archives were subsequently signed). The arm64 archive was
# independently downloaded and its digest checked before adding these pins.
# https://github.com/cloudflare/cloudflared/releases/tag/2026.8.3
_ASSETS = {
    ("Darwin", "arm64"): _Asset(
        "cloudflared-darwin-arm64.tgz",
        "40c9144d86df8937c5b43293a1f7d2d2107029aa74725023dd46b1b27154352f",
    ),
    ("Darwin", "amd64"): _Asset(
        "cloudflared-darwin-amd64.tgz",
        "61e1316266a00fd70ce40da011d612badc805367fb65293dd1925f938f704c99",
    ),
    ("Linux", "amd64"): _Asset(
        "cloudflared-linux-amd64",
        "f29324fe934d1e100617484c78deef803c4dc2cd351d645bbde42e96b4fccc5e",
    ),
    ("Linux", "arm64"): _Asset(
        "cloudflared-linux-arm64",
        "4bcfd35521a7cbc545ebfd5d57334a71ee180e2a64874981f374c81472118391",
    ),
    ("Windows", "amd64"): _Asset(
        "cloudflared-windows-amd64.exe",
        "83e726ed18ea78c5ad5213c4c3a3a27051393950d2bc8ed4de69bec12d14eaae",
    ),
}
_URL = re.compile(
    r"(?<!\S)https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"\.trycloudflare\.com(?=\s|$)"
)


def _asset() -> _Asset:
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
    selected = _ASSETS.get((platform.system(), arch.get(machine, "")))
    if selected is None:
        raise TunnelError(
            "Automatic tunnel supports macOS/Linux arm64 or x64, and Windows x64. "
            "Use a separately managed HTTPS origin on this platform."
        )
    return selected


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise TunnelError("Tunnel cache must be a private directory, not a symlink.")
    if os.name == "posix":
        if path.stat().st_uid != os.getuid():
            raise TunnelError("Tunnel cache must belong to the current user.")
        path.chmod(0o700)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_limited(stream: IO[bytes]) -> bytes:
    chunks: list[bytes] = []
    size = 0
    deadline = time.monotonic() + DOWNLOAD_DEADLINE_SECONDS
    while True:
        if time.monotonic() >= deadline:
            raise TunnelError("Tunnel helper download timed out; try again.")
        chunk = stream.read(min(1024 * 1024, MAX_DOWNLOAD_BYTES - size + 1))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_DOWNLOAD_BYTES:
            raise TunnelError("Tunnel helper exceeds the allowed download size.")


def _cached_bytes(path: Path) -> bytes | None:
    if not os.path.lexists(path):
        return None
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise TunnelError("Tunnel cache contains an unsafe file; use a new private cache.")
    with path.open("rb") as handle:
        return _read_limited(handle)


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    fd, name = tempfile.mkstemp(prefix=".download-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _executable_bytes(asset: _Asset, archive: bytes) -> bytes:
    if not asset.name.endswith(".tgz"):
        return archive
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
            members = bundle.getmembers()
            if (
                len(members) != 1
                or members[0].name != "cloudflared"
                or not members[0].isfile()
                or not 0 < members[0].size <= MAX_DOWNLOAD_BYTES
            ):
                raise TunnelError("Tunnel helper archive has unexpected contents.")
            handle = bundle.extractfile(members[0])
            if handle is None:
                raise TunnelError("Tunnel helper archive has no executable.")
            with handle:
                return _read_limited(handle)
    except (tarfile.TarError, EOFError) as exc:
        raise TunnelError("Tunnel helper archive is invalid.") from exc


def _install_binary(cache_dir: Path) -> tuple[Path, str]:
    asset = _asset()
    _private_directory(cache_dir)
    version_dir = cache_dir / CLOUDFLARED_VERSION
    _private_directory(version_dir)
    directory = version_dir / asset.name.replace(".", "-")
    _private_directory(directory)
    archive_path = directory / "download"
    archive = _cached_bytes(archive_path)
    if archive is None:
        url = (
            "https://github.com/cloudflare/cloudflared/releases/download/"
            f"{CLOUDFLARED_VERSION}/{asset.name}"
        )
        try:
            with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
                archive = _read_limited(response)
        except (OSError, urllib.error.URLError) as exc:
            raise TunnelError("Could not download the official tunnel helper; try again.") from exc
        if _digest(archive) != asset.sha256:
            raise TunnelError("Tunnel helper download failed SHA256 verification; not executed.")
        _atomic_write(archive_path, archive, 0o600)
    if _digest(archive) != asset.sha256:
        raise TunnelError(
            "Tunnel helper cache failed SHA256 verification; remove its cache and retry."
        )
    executable = _executable_bytes(asset, archive)
    binary = directory / ("cloudflared.exe" if asset.name.endswith(".exe") else "cloudflared")
    expected = _digest(executable)
    installed = _cached_bytes(binary)
    if installed is None:
        _atomic_write(binary, executable, 0o700)
    elif _digest(installed) != expected:
        raise TunnelError(
            "Tunnel executable failed SHA256 verification; remove its cache and retry."
        )
    return binary, expected


def _helper_environment() -> dict[str, str]:
    # Do not pass API keys or user cloudflared token/settings environment to a
    # third-party subprocess. Preserve only basic OS/runtime lookup variables.
    allowed = {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "TMPDIR",
        "HOME",
        "USERPROFILE",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


class QuickTunnel:
    """Own a temporary account-free HTTPS tunnel, closing it on context exit.

    A URL announcement is not a public-health check. The caller must verify its
    own app through ``url`` before offering connector setup or enabling dialing.
    """

    def __init__(self, port: int, cache_dir: Path) -> None:
        if isinstance(port, bool) or not 1 <= port <= 65535:
            raise TunnelError("Tunnel port must be between 1 and 65535.")
        self.port = port
        self.cache_dir = cache_dir
        self.url = ""
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._announced = threading.Event()
        self._finished = threading.Event()
        self._run_dir: tempfile.TemporaryDirectory[str] | None = None
        self._used = False

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def __enter__(self) -> Self:
        if self._used:
            raise TunnelError("Create a new QuickTunnel context for each run.")
        self._used = True
        try:
            binary, expected = _install_binary(self.cache_dir)
            self._run_dir = tempfile.TemporaryDirectory(prefix="run-", dir=self.cache_dir)
            config = Path(self._run_dir.name) / "config.yml"
            _atomic_write(config, b"{}\n", 0o600)
            # Check the executable immediately before launch, including cached runs.
            data = _cached_bytes(binary)
            if data is None or _digest(data) != expected:
                raise TunnelError("Tunnel executable failed final verification; not executed.")
            self._process = subprocess.Popen(
                [
                    str(binary.resolve()),
                    "tunnel",
                    "--config",
                    str(config.resolve()),
                    "--no-autoupdate",
                    "--protocol",
                    "http2",
                    "--url",
                    f"http://127.0.0.1:{self.port}",
                    "--metrics",
                    "127.0.0.1:0",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=_helper_environment(),
                start_new_session=os.name != "nt",
            )
            self._reader = threading.Thread(target=self._read_output, daemon=True)
            self._reader.start()
            deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                if not self.is_running or self._finished.is_set():
                    raise TunnelError(
                        "Tunnel helper stopped before setup; check your network and retry."
                    )
                if self._announced.wait(timeout=0.1):
                    if not self.is_running:
                        raise TunnelError("Tunnel helper stopped before setup; try again.")
                    return self
            raise TunnelError("Tunnel startup timed out; check your network and retry.")
        except BaseException as exc:
            self.close()
            if isinstance(exc, OSError):
                raise TunnelError("Could not install or start the managed tunnel helper.") from exc
            raise

    def _read_output(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        try:
            while line := process.stdout.readline(4096):
                if not self.url:
                    match = _URL.search(line)
                    if match is not None:
                        self.url = match.group(0)
                        self._announced.set()
        except (OSError, ValueError):
            pass
        finally:
            self._finished.set()

    def close(self) -> None:
        process = self._process
        try:
            if process is not None:
                if process.poll() is None:
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        pass
                try:
                    process.wait(timeout=STOP_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=STOP_TIMEOUT_SECONDS)
                if self._reader is not None:
                    self._reader.join(timeout=STOP_TIMEOUT_SECONDS)
                if process.stdout is not None:
                    process.stdout.close()
        finally:
            if self._run_dir is not None:
                self._run_dir.cleanup()
                self._run_dir = None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
