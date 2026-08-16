"""Finding #8: Qdrant client must be able to authenticate, and a public,
unauthenticated-looking config must be loudly flagged (not silently
allowed, but also not a hard startup failure — that exact config might
already be firewalled at the network level, which this process can't
observe, so this stays a warning, not a raise)."""
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.search import indexer
from app.services.search.indexer import _looks_private, get_qdrant


def test_looks_private_localhost():
    assert _looks_private("http://localhost:7333") is True


def test_looks_private_rfc1918():
    assert _looks_private("http://10.0.0.5:7333") is True
    assert _looks_private("http://192.168.1.5:7333") is True


def test_looks_private_public_ip_is_false():
    assert _looks_private("http://76.13.17.48:7333") is False


def test_get_qdrant_passes_api_key_when_configured():
    indexer._client = None
    with patch("app.services.search.indexer.settings") as mock_settings, \
         patch("app.services.search.indexer.AsyncQdrantClient") as mock_ctor:
        mock_settings.QDRANT_URL = "http://76.13.17.48:7333"
        mock_settings.QDRANT_API_KEY = "real-key-123"
        get_qdrant()
        _, kwargs = mock_ctor.call_args
        assert kwargs["api_key"] == "real-key-123"
    indexer._client = None


def test_get_qdrant_warns_on_public_url_with_no_api_key():
    indexer._client = None
    with patch("app.services.search.indexer.settings") as mock_settings, \
         patch("app.services.search.indexer.AsyncQdrantClient"), \
         patch("app.services.search.indexer.logger") as mock_logger:
        mock_settings.QDRANT_URL = "http://76.13.17.48:7333"
        mock_settings.QDRANT_API_KEY = None
        get_qdrant()
        assert mock_logger.warning.called
    indexer._client = None


def test_get_qdrant_no_warning_for_private_url():
    indexer._client = None
    with patch("app.services.search.indexer.settings") as mock_settings, \
         patch("app.services.search.indexer.AsyncQdrantClient"), \
         patch("app.services.search.indexer.logger") as mock_logger:
        mock_settings.QDRANT_URL = "http://localhost:7333"
        mock_settings.QDRANT_API_KEY = None
        get_qdrant()
        assert not mock_logger.warning.called
    indexer._client = None
