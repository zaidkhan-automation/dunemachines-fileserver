"""Tests for UploadService._detect_asset_type — specifically the 2026-09-07
Parquet fix.

Root cause (found live, real Omnius session): Parquet has no IANA-registered
MIME type. dunemachines_backend's fileserver_sync.py MIME_MAP had no
".parquet" entry, so guess_mime() fell back to "text/plain" for every
generated Parquet artifact — which used to make it past this function's
mime.startswith("text/") branch into AssetType.CODE. Confirmed live:
mime_type=text/plain, asset_type=code for a real analysis_*.parquet asset.

AssetType.DATASET already existed in the taxonomy (app/models/asset.py) but
was never reachable from _detect_asset_type. Fixed by checking the filename
extension directly for .parquet — this function already received `filename`
but never used it — rather than depending on every caller (this client
included) sending a correct mime_type for a format with no standard one.
"""
from app.services.uploads.upload_service import UploadService
from app.models.asset import AssetType

_svc = UploadService()


def test_parquet_with_correct_mime_classified_as_dataset():
    assert _svc._detect_asset_type("application/vnd.apache.parquet", "analysis_abc123.parquet") == AssetType.DATASET


def test_parquet_with_text_plain_fallback_mime_still_classified_as_dataset():
    """The exact bug: a caller sending the generic text/plain fallback for
    a .parquet file must no longer land on AssetType.CODE."""
    assert _svc._detect_asset_type("text/plain", "analysis_abc123.parquet") == AssetType.DATASET


def test_parquet_uppercase_extension_classified_as_dataset():
    assert _svc._detect_asset_type("text/plain", "DATA.PARQUET") == AssetType.DATASET


def test_parquet_nested_path_classified_as_dataset():
    assert _svc._detect_asset_type("text/plain", "orgs/x/uploads/nested/data.parquet") == AssetType.DATASET


# ── Regression: every other branch stays exactly as before ───────────────

def test_csv_still_classified_as_spreadsheet():
    assert _svc._detect_asset_type("text/csv", "data.csv") == AssetType.SPREADSHEET


def test_image_still_classified_as_image():
    assert _svc._detect_asset_type("image/png", "photo.png") == AssetType.IMAGE


def test_video_still_classified_as_video():
    assert _svc._detect_asset_type("video/mp4", "clip.mp4") == AssetType.VIDEO


def test_pdf_still_classified_as_document():
    assert _svc._detect_asset_type("application/pdf", "report.pdf") == AssetType.DOCUMENT


def test_plain_text_non_parquet_still_classified_as_code():
    """The mime.startswith("text/") -> CODE branch must be unchanged for
    every file that isn't a .parquet — this fix must be additive only."""
    assert _svc._detect_asset_type("text/plain", "notes.txt") == AssetType.CODE


def test_unknown_mime_still_falls_back_to_file():
    assert _svc._detect_asset_type("application/octet-stream", "mystery.bin") == AssetType.FILE


def test_json_still_classified_as_code():
    assert _svc._detect_asset_type("application/json", "data.json") == AssetType.CODE
