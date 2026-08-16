"""
Findings #4 (MIME normalization) and #3-corrected (unsanitized upload
filename) from the targeted fileserver hardening pass, 2026-08.
"""
import pytest
from pydantic import ValidationError

from app.api.rest.uploads import InitUploadRequest


def _make(filename="ok.txt", mime_type="text/plain", size_bytes=10):
    return InitUploadRequest(filename=filename, mime_type=mime_type, size_bytes=size_bytes)


def test_mime_type_normalized_lowercase_and_charset_stripped():
    req = _make(mime_type="TEXT/PLAIN; charset=utf-8")
    assert req.mime_type == "text/plain"


def test_mime_type_bare_lowercase_unchanged():
    req = _make(mime_type="application/json")
    assert req.mime_type == "application/json"


def test_filename_path_traversal_stripped_to_basename():
    req = _make(filename="../../etc/passwd")
    assert req.filename == "passwd"


def test_filename_windows_separator_stripped_to_basename():
    req = _make(filename="C:\\Users\\x\\secret.txt")
    assert req.filename == "secret.txt"


def test_filename_control_and_quote_chars_stripped():
    req = _make(filename='evil"; rm -rf /\x00.txt')
    assert '"' not in req.filename
    assert "\x00" not in req.filename


def test_filename_normal_name_unchanged():
    req = _make(filename="my-report_v2.pdf")
    assert req.filename == "my-report_v2.pdf"


def test_filename_dotfile_preserved():
    """Leading dots are legitimate (.gitignore, .env.example) — only
    traversal sequences and path separators are stripped, not dots."""
    req = _make(filename=".gitignore")
    assert req.filename == ".gitignore"


def test_filename_empty_after_sanitization_rejected():
    with pytest.raises(ValidationError):
        _make(filename="../../")
