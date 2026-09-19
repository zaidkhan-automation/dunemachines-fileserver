"""
Finding #8 (revised, 2026-08): Qdrant confirmed genuinely reachable on a
non-localhost address with no auth via live netstat on the production
server — this is a hard RuntimeError at config-load time, not a
warning. Only localhost/127.0.0.1/unix:// are treated as safe without
QDRANT_API_KEY set.

qdrant_requires_api_key() is unit-tested directly (pure function, no
import-time side effects to fight with). The actual startup crash is
tested via a real subprocess — the most faithful way to confirm "fails
at process startup" without the fragility of mutating os.environ and
reimporting a pydantic-settings module in-process (module caching and
env-var precedence made that approach flaky).
"""
import os
import subprocess
import sys

from app.core.config import qdrant_requires_api_key


def test_localhost_does_not_require_key():
    assert qdrant_requires_api_key("http://localhost:6333") is False


def test_127_0_0_1_does_not_require_key():
    assert qdrant_requires_api_key("http://127.0.0.1:6333") is False


def test_unix_socket_does_not_require_key():
    assert qdrant_requires_api_key("unix:///var/run/qdrant.sock") is False


def test_public_hostname_requires_key():
    assert qdrant_requires_api_key("http://qdrant.example.com:6333") is True


def test_public_ip_requires_key():
    assert qdrant_requires_api_key("http://76.13.17.48:7333") is True


def test_qdrant_url_default_is_localhost_not_a_stale_external_address():
    """Permanent regression lock, 2026-09-19 production incident: this
    exact default previously WAS "http://76.13.17.48:7333" -- an address
    that is not reachable from this host and was never the actual
    canonical Qdrant instance. It silently shadowed a correct
    127.0.0.1:7333 value in .env too (a duplicate key, dotenv's
    last-key-wins semantics), and because neither disagreed, nothing ever
    surfaced the mismatch. Left ~89% of this app's "ready" assets (1385 of
    1555 at the time) with no Qdrant embedding at all -- no exception,
    no crash, just silently unsearchable, for as long as this default
    existed. This test's only job is to make sure a future edit can never
    quietly reintroduce a non-localhost default here without deliberately
    changing this test in the same commit."""
    from app.core.config import Settings
    default_url = Settings.model_fields["QDRANT_URL"].default
    assert qdrant_requires_api_key(default_url) is False, (
        f"QDRANT_URL's default ({default_url!r}) is a non-localhost address -- "
        "this is exactly the production incident this test exists to prevent. "
        "If this default genuinely needs to change, update it deliberately and "
        "update this test in the same commit, don't just delete the assertion."
    )


def _run_with_env(extra_env: dict) -> subprocess.CompletedProcess:
    env = {**os.environ, **extra_env}
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return subprocess.run(
        [sys.executable, "-c", "from app.core.config import settings"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_process_actually_fails_to_start_with_public_url_and_no_key():
    result = _run_with_env({
        "QDRANT_URL": "http://qdrant.example.com:6333",
        "QDRANT_API_KEY": "",
    })
    assert result.returncode != 0
    assert "QDRANT_API_KEY" in result.stderr


def test_process_starts_fine_with_public_url_and_a_key():
    result = _run_with_env({
        "QDRANT_URL": "http://qdrant.example.com:6333",
        "QDRANT_API_KEY": "real-key-abc",
    })
    assert result.returncode == 0, result.stderr


def test_process_starts_fine_with_localhost_and_no_key():
    result = _run_with_env({
        "QDRANT_URL": "http://localhost:6333",
        "QDRANT_API_KEY": "",
    })
    assert result.returncode == 0, result.stderr
