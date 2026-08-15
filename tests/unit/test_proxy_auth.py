"""Unit tests for the Anthropic-codex proxy auth layer.

Covers the shared `_proxy_auth.py` module and the Handler-level integration
in both `anthropic-request-normalizer` and `codex-chatgpt-openai-shim`.

Tests fake the BaseHTTPRequestHandler interface (headers + send_*/wfile) with
lightweight stand-ins so we never spin up a real HTTP server in unit tests.
"""

from __future__ import annotations

import importlib.util
import io
import os

# axi.config validates the discord token at import time. Set a placeholder
# before any `from axi import ...` so module-load succeeds in unit tests.
os.environ.setdefault("DISCORD_TOKEN", "unit-test-fake-discord-token")

from importlib.machinery import SourceFileLoader
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType
    from typing import Any

SCRIPTS_DIR = Path(__file__).parents[2] / "scripts" / "anthropic-codex-proxy"


def load_module(name: str, file_basename: str) -> ModuleType:
    """Source-load one of the proxy scripts (no .py extension)."""
    path = SCRIPTS_DIR / file_basename
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def proxy_auth() -> ModuleType:
    """Load `_proxy_auth.py` directly (no script-import dance needed)."""
    path = SCRIPTS_DIR / "_proxy_auth.py"
    loader = SourceFileLoader("_proxy_auth_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def normalizer() -> ModuleType:
    return load_module("normalizer_test", "anthropic-request-normalizer")


VALID_TOKEN = "0" * 64  # 64 hex chars (canonical secrets.token_hex(32) length)


class FakeHeaders:
    """Mimic the case-insensitive .get() interface of http.server headers."""

    def __init__(self, headers: dict[str, str]):
        self._h = {k.lower(): v for k, v in headers.items()}

    def get(self, key: str, default: str | None = None) -> str | None:
        return self._h.get(key.lower(), default)


class FakeHandler:
    """Minimal stand-in for BaseHTTPRequestHandler used by the auth helpers."""

    def __init__(self, headers: dict[str, str], path: str = "/v1/messages"):
        self.headers = FakeHeaders(headers)
        self.path = path
        self.wfile = io.BytesIO()
        self._status: int | None = None
        self._sent_headers: list[tuple[str, str]] = []

    def send_response(self, code: int) -> None:
        self._status = code

    def send_header(self, key: str, value: str) -> None:
        self._sent_headers.append((key, value))

    def end_headers(self) -> None:
        pass

    @property
    def status(self) -> int | None:
        return self._status

    def body_text(self) -> str:
        return self.wfile.getvalue().decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# A.1 token validation: valid / wrong / missing / empty / wrong-format
# ---------------------------------------------------------------------------


def test_valid_token_x_api_key_accepted(proxy_auth: ModuleType) -> None:
    handler = FakeHandler({"x-api-key": VALID_TOKEN})
    assert proxy_auth.check_token(handler, VALID_TOKEN) is True
    assert handler.status is None  # nothing was sent on success


def test_wrong_token_rejected_with_401(proxy_auth: ModuleType) -> None:
    handler = FakeHandler({"x-api-key": "f" * 64})
    assert proxy_auth.check_token(handler, VALID_TOKEN) is False
    assert handler.status == 401
    assert "unauthorized" in handler.body_text()


def test_missing_token_rejected_with_401(proxy_auth: ModuleType) -> None:
    handler = FakeHandler({})
    assert proxy_auth.check_token(handler, VALID_TOKEN) is False
    assert handler.status == 401


def test_empty_token_rejected_with_401(proxy_auth: ModuleType) -> None:
    handler = FakeHandler({"x-api-key": ""})
    assert proxy_auth.check_token(handler, VALID_TOKEN) is False
    assert handler.status == 401


def test_wrong_format_token_rejected_with_401(proxy_auth: ModuleType) -> None:
    # Right length-ish, wrong content
    handler = FakeHandler({"x-api-key": "not-a-real-token"})
    assert proxy_auth.check_token(handler, VALID_TOKEN) is False
    assert handler.status == 401


# ---------------------------------------------------------------------------
# A.2 both header forms accepted
# ---------------------------------------------------------------------------


def test_authorization_bearer_form_accepted(proxy_auth: ModuleType) -> None:
    handler = FakeHandler({"authorization": f"Bearer {VALID_TOKEN}"})
    assert proxy_auth.check_token(handler, VALID_TOKEN) is True


def test_lowercase_bearer_prefix_accepted(proxy_auth: ModuleType) -> None:
    handler = FakeHandler({"authorization": f"bearer {VALID_TOKEN}"})
    assert proxy_auth.check_token(handler, VALID_TOKEN) is True


def test_bearer_with_wrong_token_rejected(proxy_auth: ModuleType) -> None:
    handler = FakeHandler({"authorization": "Bearer wrong"})
    assert proxy_auth.check_token(handler, VALID_TOKEN) is False
    assert handler.status == 401


def test_non_bearer_authorization_ignored(proxy_auth: ModuleType) -> None:
    # "Basic" auth header is not a bearer — should be ignored, no fallback.
    handler = FakeHandler({"authorization": f"Basic {VALID_TOKEN}"})
    assert proxy_auth.check_token(handler, VALID_TOKEN) is False
    assert handler.status == 401


# ---------------------------------------------------------------------------
# A.3 hmac.compare_digest is used (not ==)
# ---------------------------------------------------------------------------


def test_check_token_uses_hmac_compare_digest(
    proxy_auth: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify the auth helper actually calls hmac.compare_digest, not ==.

    Patches hmac.compare_digest to a sentinel and asserts it was invoked.
    """
    import hmac

    calls: list[tuple[bytes, bytes]] = []

    def fake_compare_digest(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return a == b

    monkeypatch.setattr(hmac, "compare_digest", fake_compare_digest)

    handler = FakeHandler({"x-api-key": VALID_TOKEN})
    assert proxy_auth.check_token(handler, VALID_TOKEN) is True
    assert calls, "hmac.compare_digest was not called by check_token"
    # The first arg is the incoming, second is expected; both are bytes.
    assert all(isinstance(a, bytes) and isinstance(b, bytes) for a, b in calls)


# ---------------------------------------------------------------------------
# A.4 do_OPTIONS returns 403 (not 200, not 501, not 204)
# ---------------------------------------------------------------------------


def test_options_handler_returns_403(proxy_auth: ModuleType) -> None:
    handler = FakeHandler({})
    proxy_auth.reject_options(handler)
    assert handler.status == 403
    assert "options_disabled" in handler.body_text()


def test_normalizer_handler_has_do_options(normalizer: ModuleType) -> None:
    """The Handler class must define do_OPTIONS so OPTIONS doesn't fall back to 501."""
    assert hasattr(normalizer.Handler, "do_OPTIONS"), "Handler must define do_OPTIONS"


# ---------------------------------------------------------------------------
# A.5 Host header allowlist
# ---------------------------------------------------------------------------


def test_host_127_0_0_1_with_port_accepted(proxy_auth: ModuleType) -> None:
    allowed = proxy_auth.allowed_host_values(3000)
    handler = FakeHandler({"host": "127.0.0.1:3000"})
    assert proxy_auth.check_host_header(handler, allowed) is True


def test_host_localhost_with_port_accepted(proxy_auth: ModuleType) -> None:
    allowed = proxy_auth.allowed_host_values(3000)
    handler = FakeHandler({"host": "localhost:3000"})
    assert proxy_auth.check_host_header(handler, allowed) is True


def test_host_uppercase_localhost_accepted(proxy_auth: ModuleType) -> None:
    """Host header is case-insensitive per RFC 7230 §5.4."""
    allowed = proxy_auth.allowed_host_values(3000)
    handler = FakeHandler({"host": "LOCALHOST:3000"})
    assert proxy_auth.check_host_header(handler, allowed) is True


def test_host_evil_com_rejected_403(proxy_auth: ModuleType) -> None:
    allowed = proxy_auth.allowed_host_values(3000)
    handler = FakeHandler({"host": "evil.com"})
    assert proxy_auth.check_host_header(handler, allowed) is False
    assert handler.status == 403
    assert "host_header_rejected" in handler.body_text()


def test_host_wrong_port_rejected_403(proxy_auth: ModuleType) -> None:
    allowed = proxy_auth.allowed_host_values(3000)
    handler = FakeHandler({"host": "127.0.0.1:9999"})
    assert proxy_auth.check_host_header(handler, allowed) is False
    assert handler.status == 403


def test_missing_host_header_rejected_403(proxy_auth: ModuleType) -> None:
    allowed = proxy_auth.allowed_host_values(3000)
    handler = FakeHandler({})
    assert proxy_auth.check_host_header(handler, allowed) is False
    assert handler.status == 403


# ---------------------------------------------------------------------------
# A.6 token auto-generation: first start writes 600+hex; second start preserves
# ---------------------------------------------------------------------------


def test_first_start_creates_token_with_mode_600(
    proxy_auth: ModuleType, tmp_path: Path
) -> None:
    token_file = tmp_path / "subdir" / "proxy-token"
    assert not token_file.exists()

    token = proxy_auth.load_or_create_token(token_file)

    assert token_file.exists()
    assert len(token) == 64
    assert all(c in "0123456789abcdef" for c in token)

    mode = os.stat(token_file).st_mode & 0o777
    assert mode == 0o600, f"expected 600, got {oct(mode)}"

    # File contents == token (with optional trailing newline)
    on_disk = token_file.read_text().strip()
    assert on_disk == token


def test_second_start_preserves_existing_token(
    proxy_auth: ModuleType, tmp_path: Path
) -> None:
    token_file = tmp_path / "proxy-token"
    first = proxy_auth.load_or_create_token(token_file)
    first_mtime = os.stat(token_file).st_mtime

    second = proxy_auth.load_or_create_token(token_file)
    second_mtime = os.stat(token_file).st_mtime

    assert first == second, "second call must return existing token verbatim"
    assert first_mtime == second_mtime, "existing file must not be rewritten"


def test_load_or_create_uses_env_var_when_path_omitted(
    proxy_auth: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "from-env"
    monkeypatch.setenv("AXI_PROXY_TOKEN_FILE", str(token_file))
    token = proxy_auth.load_or_create_token()
    assert token_file.exists()
    assert len(token) == 64


# ---------------------------------------------------------------------------
# A.7 token file permission check: mode > 600 rejected
# ---------------------------------------------------------------------------


def test_world_readable_token_file_rejected(
    proxy_auth: ModuleType, tmp_path: Path
) -> None:
    token_file = tmp_path / "proxy-token"
    token_file.write_text(VALID_TOKEN + "\n")
    os.chmod(token_file, 0o644)  # world-readable — sloppy install

    with pytest.raises(SystemExit) as exc_info:
        proxy_auth.load_or_create_token(token_file)
    assert "insecure mode" in str(exc_info.value)


def test_group_readable_token_file_rejected(
    proxy_auth: ModuleType, tmp_path: Path
) -> None:
    token_file = tmp_path / "proxy-token"
    token_file.write_text(VALID_TOKEN + "\n")
    os.chmod(token_file, 0o640)  # group-readable

    with pytest.raises(SystemExit) as exc_info:
        proxy_auth.load_or_create_token(token_file)
    assert "insecure mode" in str(exc_info.value)


def test_malformed_token_file_rejected(
    proxy_auth: ModuleType, tmp_path: Path
) -> None:
    token_file = tmp_path / "proxy-token"
    token_file.write_text("not-hex\n")
    os.chmod(token_file, 0o600)

    with pytest.raises(SystemExit) as exc_info:
        proxy_auth.load_or_create_token(token_file)
    assert "malformed" in str(exc_info.value)


def test_load_token_missing_file_raises(proxy_auth: ModuleType, tmp_path: Path) -> None:
    token_file = tmp_path / "does-not-exist"
    with pytest.raises(SystemExit) as exc_info:
        proxy_auth.load_token(token_file)
    assert "not found" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Axi config integration — token-file precedence & error paths
# ---------------------------------------------------------------------------


def test_axi_config_reads_token_from_env_var_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """axi.config._chatgpt_proxy_env() pulls the token from AXI_PROXY_TOKEN_FILE."""
    from axi import config

    token_file = tmp_path / "proxy-token"
    token_file.write_text(VALID_TOKEN + "\n")
    os.chmod(token_file, 0o600)

    monkeypatch.setenv("AXI_PROXY_TOKEN_FILE", str(token_file))
    monkeypatch.delenv("AXI_CHATGPT_PROXY_API_KEY", raising=False)
    # _chatgpt_proxy_env reads ANTHROPIC_API_KEY from the import-time
    # _ENV_SNAPSHOT, which delenv cannot affect — clear it so the token
    # file is the only fallback.
    monkeypatch.setitem(config._ENV_SNAPSHOT, "ANTHROPIC_API_KEY", None)

    env = config._chatgpt_proxy_env("gpt-5.4")
    assert env["ANTHROPIC_API_KEY"] == VALID_TOKEN


def test_axi_config_env_override_wins_over_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from axi import config

    token_file = tmp_path / "proxy-token"
    token_file.write_text(VALID_TOKEN + "\n")
    os.chmod(token_file, 0o600)

    monkeypatch.setenv("AXI_PROXY_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("AXI_CHATGPT_PROXY_API_KEY", "explicit-override")
    # Pin the snapshot so an ambient ANTHROPIC_API_KEY at import cannot
    # shadow the explicit override chain being tested.
    monkeypatch.setitem(config._ENV_SNAPSHOT, "ANTHROPIC_API_KEY", None)

    env = config._chatgpt_proxy_env("gpt-5.4")
    assert env["ANTHROPIC_API_KEY"] == "explicit-override"


def test_axi_config_raises_when_token_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from axi import config

    nonexistent = tmp_path / "nope"
    monkeypatch.setenv("AXI_PROXY_TOKEN_FILE", str(nonexistent))
    monkeypatch.delenv("AXI_CHATGPT_PROXY_API_KEY", raising=False)
    monkeypatch.setitem(config._ENV_SNAPSHOT, "ANTHROPIC_API_KEY", None)

    with pytest.raises(RuntimeError) as exc_info:
        config._chatgpt_proxy_env("gpt-5.4")
    assert "proxy token file" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Handler-level integration — verify the normalizer rejects bad requests
# without forwarding to the upstream target
# ---------------------------------------------------------------------------


class CapturingHandler:
    """Build an instance of normalizer.Handler without touching the network.

    BaseHTTPRequestHandler.__init__ wants a socket; we bypass it by constructing
    via __new__ and setting the attributes the auth path needs.
    """

    @staticmethod
    def make(normalizer_module: ModuleType, headers: dict[str, str], path: str = "/v1/messages") -> Any:
        instance = normalizer_module.Handler.__new__(normalizer_module.Handler)
        instance.headers = FakeHeaders(headers)
        instance.path = path
        instance.wfile = io.BytesIO()
        instance._status = None
        instance._sent_headers = []

        def send_response(code: int) -> None:
            instance._status = code

        def send_header(k: str, v: str) -> None:
            instance._sent_headers.append((k, v))

        def end_headers() -> None:
            pass

        instance.send_response = send_response  # type: ignore[method-assign]
        instance.send_header = send_header  # type: ignore[method-assign]
        instance.end_headers = end_headers  # type: ignore[method-assign]
        return instance


def test_normalizer_post_without_token_returns_401(normalizer: ModuleType) -> None:
    normalizer.Handler.expected_token = VALID_TOKEN
    normalizer.Handler.allowed_hosts = {"127.0.0.1:3000", "localhost:3000"}

    handler = CapturingHandler.make(
        normalizer,
        {"host": "127.0.0.1:3000", "content-length": "0"},
        path="/v1/messages",
    )
    handler.do_POST()
    assert handler._status == 401


def test_normalizer_post_with_evil_host_returns_403_before_auth(
    normalizer: ModuleType,
) -> None:
    normalizer.Handler.expected_token = VALID_TOKEN
    normalizer.Handler.allowed_hosts = {"127.0.0.1:3000", "localhost:3000"}

    handler = CapturingHandler.make(
        normalizer,
        {
            "host": "evil.com",
            "content-length": "0",
            # Even with a valid token, host check fails first
            "x-api-key": VALID_TOKEN,
        },
        path="/v1/messages",
    )
    handler.do_POST()
    assert handler._status == 403


def test_normalizer_options_returns_403(normalizer: ModuleType) -> None:
    normalizer.Handler.expected_token = VALID_TOKEN
    normalizer.Handler.allowed_hosts = {"127.0.0.1:3000", "localhost:3000"}

    handler = CapturingHandler.make(normalizer, {"host": "127.0.0.1:3000"})
    handler.do_OPTIONS()
    assert handler._status == 403


def test_normalizer_health_endpoint_skips_auth(normalizer: ModuleType) -> None:
    normalizer.Handler.expected_token = VALID_TOKEN
    normalizer.Handler.allowed_hosts = {"127.0.0.1:3000", "localhost:3000"}

    handler = CapturingHandler.make(normalizer, {"host": "127.0.0.1:3000"}, path="/health")
    handler.do_GET()
    assert handler._status == 200
