"""Finding #2: presigned-URL expiry must be clamped to [5min, 24h]."""
from app.services.uploads.upload_service import _clamp_expiry, _MIN_EXPIRY_SECONDS, _MAX_EXPIRY_SECONDS


def test_default_3600_unaffected():
    assert _clamp_expiry(3600) == 3600


def test_too_short_clamped_to_minimum():
    assert _clamp_expiry(10) == _MIN_EXPIRY_SECONDS


def test_too_long_clamped_to_maximum():
    assert _clamp_expiry(999999999) == _MAX_EXPIRY_SECONDS


def test_negative_clamped_to_minimum():
    assert _clamp_expiry(-1) == _MIN_EXPIRY_SECONDS
