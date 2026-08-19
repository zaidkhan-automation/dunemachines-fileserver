"""
Regression tests for the 2026-08-19 production bug: app/events/consumers.py's
HANDLERS registry used to be a single dict slot (`HANDLERS[event_type] = func`),
so when two different workers subscribed to the same EventType.ASSET_UPLOAD_COMPLETE
(app.workers.ai.embeddings and app.workers.files.thumbnails), whichever module
was imported last silently overwrote the other's registration. embeddings.py's
handler — the only code in the app that ever advances an asset's status to
READY, or triggers AI summary generation — never ran for a single real upload
since this repo's initial commit. No exception was ever raised anywhere, so
nothing showed up in logs; the only symptom was ~378 real production assets
permanently stuck at status='processing' (some for 3+ months).

These tests assert the fixed property directly: multiple handlers registered
against the same event type must ALL run, not just the last one registered.
"""
import asyncio

import pytest

from app.events.consumers import HANDLERS, on_event
from app.events.event_types import EventType


@pytest.fixture(autouse=True)
def _clean_handlers():
    """HANDLERS is a module-global registry populated at import time by the
    real app.workers.* modules — don't let a test's throwaway registrations
    leak into other tests or into the real dispatch table."""
    snapshot = {k: list(v) for k, v in HANDLERS.items()}
    yield
    HANDLERS.clear()
    HANDLERS.update(snapshot)


@pytest.mark.asyncio
async def test_two_handlers_on_the_same_event_type_both_run():
    calls = []

    @on_event(EventType.ASSET_UPLOAD_COMPLETE)
    async def handler_a(payload):
        calls.append(("a", payload))

    @on_event(EventType.ASSET_UPLOAD_COMPLETE)
    async def handler_b(payload):
        calls.append(("b", payload))

    handlers = HANDLERS[EventType.ASSET_UPLOAD_COMPLETE.value]
    assert handler_a in handlers and handler_b in handlers, (
        "second registration overwrote the first — this is the exact bug "
        "that silently dropped app.workers.ai.embeddings' handler in production"
    )

    await asyncio.gather(*(h({"x": 1}) for h in handlers))
    assert ("a", {"x": 1}) in calls
    assert ("b", {"x": 1}) in calls


@pytest.mark.asyncio
async def test_real_embeddings_and_thumbnails_handlers_both_registered():
    """Import the two real production workers that collided on this event
    type and assert both survive registration — guards against this exact
    regression reappearing with the real modules, not just synthetic ones."""
    import app.workers.ai.embeddings as embeddings_worker
    import app.workers.files.thumbnails as thumbnails_worker

    handlers = HANDLERS.get(EventType.ASSET_UPLOAD_COMPLETE.value, [])
    assert embeddings_worker.handle_upload_complete in handlers
    assert thumbnails_worker.handle_thumbnail_generation in handlers
