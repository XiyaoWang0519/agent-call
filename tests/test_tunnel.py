from __future__ import annotations

import hashlib
import io
import os
import queue
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

from app import tunnel


def _download(
    monkeypatch, payload=b"fake executable", *, digest=None, name="cloudflared-linux-amd64"
):
    asset = tunnel._Asset(name, digest or hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(tunnel, "_asset", lambda: asset)
    requests = []

    def fetch(url, *, timeout):
        requests.append((url, timeout))
        return io.BytesIO(payload)

    monkeypatch.setattr(tunnel.urllib.request, "urlopen", fetch)
    return requests


def test_download_is_pinned_cached_and_private(tmp_path, monkeypatch):
    requests = _download(monkeypatch)
    binary, expected = tunnel._install_binary(tmp_path)
    assert binary.read_bytes() == b"fake executable"
    assert expected == hashlib.sha256(binary.read_bytes()).hexdigest()
    assert requests == [
        (
            "https://github.com/cloudflare/cloudflared/releases/download/2026.8.3/"
            "cloudflared-linux-amd64",
            tunnel.DOWNLOAD_TIMEOUT_SECONDS,
        )
    ]
    assert tunnel._install_binary(tmp_path) == (binary, expected)
    assert len(requests) == 1
    if os.name == "posix":
        assert stat.S_IMODE(binary.stat().st_mode) == 0o700
        assert stat.S_IMODE(binary.parent.stat().st_mode) == 0o700


def test_bad_download_digest_is_never_installed(tmp_path, monkeypatch):
    _download(monkeypatch, digest="0" * 64)
    with pytest.raises(tunnel.TunnelError, match="SHA256"):
        tunnel._install_binary(tmp_path)
    assert not list(tmp_path.rglob("download"))
    assert not list(tmp_path.rglob("cloudflared"))


@pytest.mark.parametrize("filename", ["download", "cloudflared"])
def test_tampered_cache_is_rejected_without_execution(tmp_path, monkeypatch, filename):
    requests = _download(monkeypatch)
    binary, _ = tunnel._install_binary(tmp_path)
    (binary.parent / filename).write_bytes(b"tampered")
    with pytest.raises(tunnel.TunnelError, match="SHA256"):
        tunnel._install_binary(tmp_path)
    assert len(requests) == 1


def test_cache_symlink_is_not_followed(tmp_path, monkeypatch):
    _download(monkeypatch)
    binary, _ = tunnel._install_binary(tmp_path)
    binary.unlink()
    victim = tmp_path / "private-file"
    victim.write_bytes(b"keep")
    binary.symlink_to(victim)
    with pytest.raises(tunnel.TunnelError, match="unsafe"):
        tunnel._install_binary(tmp_path)
    assert victim.read_bytes() == b"keep"


def test_download_size_is_bounded(monkeypatch):
    monkeypatch.setattr(tunnel, "MAX_DOWNLOAD_BYTES", 4)
    with pytest.raises(tunnel.TunnelError, match="size"):
        tunnel._read_limited(io.BytesIO(b"12345"))


def _archive(name="cloudflared", *, symlink=False):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as bundle:
        member = tarfile.TarInfo(name)
        if symlink:
            member.type = tarfile.SYMTYPE
            member.linkname = "/tmp/other"
        else:
            member.size = 3
        bundle.addfile(member, None if symlink else io.BytesIO(b"exe"))
    return output.getvalue()


def test_mac_archive_extracts_only_expected_executable(tmp_path, monkeypatch):
    _download(monkeypatch, _archive(), name="cloudflared-darwin-arm64.tgz")
    binary, _ = tunnel._install_binary(tmp_path)
    assert binary.read_bytes() == b"exe"


@pytest.mark.parametrize("name,symlink", [("../escape", False), ("cloudflared", True)])
def test_mac_archive_rejects_unsafe_members(name, symlink):
    asset = tunnel._Asset("cloudflared-darwin-arm64.tgz", "unused")
    with pytest.raises(tunnel.TunnelError, match="unexpected"):
        tunnel._executable_bytes(asset, _archive(name, symlink=symlink))


def test_unsupported_platform_fails_before_network(monkeypatch):
    monkeypatch.setattr(tunnel.platform, "system", lambda: "Windows")
    monkeypatch.setattr(tunnel.platform, "machine", lambda: "arm64")
    with pytest.raises(tunnel.TunnelError, match="separately managed"):
        tunnel._asset()


class _Output:
    def __init__(self, lines):
        self.lines = queue.Queue()
        for line in lines:
            self.lines.put(line)
        self.closed = False

    def readline(self, limit):
        assert limit == 4096
        return self.lines.get(timeout=5)

    def close(self):
        self.closed = True


class _Process:
    def __init__(self, lines, *, stubborn=False):
        self.stdout = _Output(lines)
        self.returncode = None
        self.stubborn = stubborn
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        if not self.stubborn:
            self.returncode = -15
            self.stdout.lines.put("")

    def wait(self, timeout):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("cloudflared", timeout)
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.stdout.lines.put("")


@pytest.mark.parametrize("stubborn", [False, True])
def test_context_owns_process_filters_environment_and_cleans_up(tmp_path, monkeypatch, stubborn):
    _download(monkeypatch)
    process = _Process(["INF | https://trial-name.trycloudflare.com |\n"], stubborn=stubborn)
    launches = []
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-pass")
    monkeypatch.setenv("TUNNEL_TOKEN", "must-not-pass-either")

    def launch(args, **kwargs):
        launches.append((args, kwargs))
        return process

    monkeypatch.setattr(tunnel.subprocess, "Popen", launch)
    context = tunnel.QuickTunnel(8123, tmp_path)
    with context as active:
        assert active.url == "https://trial-name.trycloudflare.com"
        assert active.is_running
        args, kwargs = launches[0]
        assert "--no-autoupdate" in args
        assert args[args.index("--protocol") + 1] == "http2"
        assert args[args.index("--url") + 1] == "http://127.0.0.1:8123"
        config = Path(args[args.index("--config") + 1])
        assert config.read_text() == "{}\n"
        assert "OPENAI_API_KEY" not in kwargs["env"]
        assert "TUNNEL_TOKEN" not in kwargs["env"]
    assert process.terminated
    assert process.killed == stubborn
    assert process.stdout.closed
    assert not context.is_running
    assert not config.exists()
    assert context._reader is not None and not context._reader.is_alive()
    context.close()


@pytest.mark.parametrize(
    "line",
    [
        "https://good.trycloudflare.com.attacker.example",
        "https://good.trycloudflare.com/extra",
        "https://user:password@good.trycloudflare.com",
        "https://good.trycloudflare.com:443",
        "https://good.trycloudflare.com?secret",
        "https://trycloudflare.com",
        "http://good.trycloudflare.com",
    ],
)
def test_url_parser_rejects_noncanonical_origins(line):
    assert tunnel._URL.search(line) is None


def test_startup_timeout_closes_process(tmp_path, monkeypatch):
    _download(monkeypatch)
    process = _Process([])
    monkeypatch.setattr(tunnel.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(tunnel, "STARTUP_TIMEOUT_SECONDS", 0)
    with pytest.raises(tunnel.TunnelError, match="timed out"):
        with tunnel.QuickTunnel(8000, tmp_path):
            pytest.fail("must not enter")
    assert process.terminated
    assert not list(tmp_path.glob("run-*"))


def test_startup_integrity_failure_never_launches(tmp_path, monkeypatch):
    _download(monkeypatch)
    binary, expected = tunnel._install_binary(tmp_path)
    binary.write_bytes(b"tampered")
    monkeypatch.setattr(tunnel, "_install_binary", lambda _: (binary, expected))
    monkeypatch.setattr(tunnel.subprocess, "Popen", lambda *a, **kw: pytest.fail("must not launch"))
    with pytest.raises(tunnel.TunnelError, match="final verification"):
        with tunnel.QuickTunnel(8000, tmp_path):
            pytest.fail("must not enter")
    assert not list(tmp_path.glob("run-*"))
