"""
Pure Pydantic-validation tests for CreateLinkRequest.expiresAt.

Regression test for a real bug caught via a live HTTP round trip against
the running service (not caught by the mocked router tests or the
real-DB service tests, since those always constructed a naive
datetime.utcnow() directly and never went through Pydantic's own
datetime parsing): a client-sent "...Z" ISO8601 string parses to a
TIMEZONE-AWARE datetime, but presentation_links.expires_at is TIMESTAMP
WITHOUT TIME ZONE — asyncpg raised DataError ("can't subtract
offset-naive and offset-aware datetimes") and the create-link endpoint
500'd. Same convention as test_upload_input_validation.py (construct the
Pydantic model directly, no DB/HTTP involved).
"""
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.api.rest.presentation_links import CreateLinkRequest


def test_z_suffixed_future_timestamp_normalizes_to_naive_utc():
    future_z = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    req = CreateLinkRequest(mode="live", expiresAt=future_z)
    assert req.expiresAt.tzinfo is None


def test_offset_suffixed_future_timestamp_normalizes_to_naive_utc():
    req = CreateLinkRequest(mode="live", expiresAt="2099-01-01T00:00:00+05:00")
    assert req.expiresAt.tzinfo is None


def test_naive_future_timestamp_accepted_unchanged():
    future = datetime.utcnow() + timedelta(hours=1)
    req = CreateLinkRequest(mode="live", expiresAt=future.isoformat())
    assert req.expiresAt.tzinfo is None


def test_z_suffixed_past_timestamp_still_rejected():
    past_z = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with pytest.raises(ValidationError, match="expiresAt must be in the future"):
        CreateLinkRequest(mode="live", expiresAt=past_z)


def test_naive_past_timestamp_still_rejected():
    past = datetime.utcnow() - timedelta(hours=1)
    with pytest.raises(ValidationError, match="expiresAt must be in the future"):
        CreateLinkRequest(mode="live", expiresAt=past.isoformat())
