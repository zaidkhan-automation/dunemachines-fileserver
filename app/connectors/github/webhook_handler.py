"""
GitHub Webhook Handler
Receives GitHub events and publishes to Event Bus.
Events: push, pull_request, issues, installation, repository
"""
import hmac
import hashlib
import logging
import json
from datetime import datetime
from typing import Dict, Any
from fastapi import APIRouter, Request, HTTPException, Header
from app.core.config import settings
from app.events.producers import publish_event
from app.events.event_types import EventType

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks/github", tags=["webhooks"])


def verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Verify GitHub webhook HMAC-SHA256 signature."""
    if not signature or not signature.startswith("sha256="):
        return False
    mac = hmac.new(secret.encode(), payload, hashlib.sha256)
    expected = "sha256=" + mac.hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/events")
async def handle_github_webhook(
    request: Request,
    x_github_event: str = Header(None),
    x_hub_signature_256: str = Header(None),
    x_github_delivery: str = Header(None),
):
    """
    Receive GitHub webhook events.
    Verifies signature, then publishes to Redis event bus.
    """
    payload_bytes = await request.body()

    # Verify signature
    secret = getattr(settings, "GITHUB_WEBHOOK_SECRET", "")
    if secret and not verify_signature(payload_bytes, x_hub_signature_256 or "", secret):
        logger.warning(f"Invalid webhook signature for delivery {x_github_delivery}")
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload = json.loads(payload_bytes)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event_type = x_github_event or "unknown"
    delivery_id = x_github_delivery or "unknown"

    logger.info(f"GitHub webhook received: {event_type} (delivery: {delivery_id})")

    # Route to appropriate handler
    handlers = {
        "push":         _handle_push,
        "pull_request": _handle_pull_request,
        "issues":       _handle_issues,
        "installation": _handle_installation,
        "repository":   _handle_repository,
        "ping":         _handle_ping,
    }

    handler = handlers.get(event_type, _handle_unknown)
    await handler(payload, delivery_id)

    return {"status": "accepted", "event": event_type, "delivery": delivery_id}


async def _handle_push(payload: Dict, delivery_id: str):
    """Handle push events — new commits."""
    repo = payload.get("repository", {})
    commits = payload.get("commits", [])
    pusher = payload.get("pusher", {})

    await publish_event(EventType.CONNECTOR_SYNC_STARTED, {
        "source": "github",
        "event": "push",
        "repo": repo.get("full_name"),
        "branch": payload.get("ref", "").replace("refs/heads/", ""),
        "commits": len(commits),
        "pusher": pusher.get("name"),
        "delivery_id": delivery_id,
    })
    logger.info(f"Push event: {repo.get('full_name')} — {len(commits)} commits")


async def _handle_pull_request(payload: Dict, delivery_id: str):
    """Handle PR events — opened, closed, merged, reviewed."""
    action = payload.get("action")
    pr = payload.get("pull_request", {})
    repo = payload.get("repository", {})

    await publish_event(EventType.CONNECTOR_SYNC_STARTED, {
        "source": "github",
        "event": "pull_request",
        "action": action,
        "repo": repo.get("full_name"),
        "pr_number": pr.get("number"),
        "pr_title": pr.get("title"),
        "pr_state": pr.get("state"),
        "merged": pr.get("merged", False),
        "delivery_id": delivery_id,
    })
    logger.info(f"PR event: {action} #{pr.get('number')} in {repo.get('full_name')}")


async def _handle_issues(payload: Dict, delivery_id: str):
    """Handle issue events — opened, closed, labeled."""
    action = payload.get("action")
    issue = payload.get("issue", {})
    repo = payload.get("repository", {})

    await publish_event(EventType.CONNECTOR_SYNC_STARTED, {
        "source": "github",
        "event": "issues",
        "action": action,
        "repo": repo.get("full_name"),
        "issue_number": issue.get("number"),
        "issue_title": issue.get("title"),
        "delivery_id": delivery_id,
    })
    logger.info(f"Issue event: {action} #{issue.get('number')} in {repo.get('full_name')}")


async def _handle_installation(payload: Dict, delivery_id: str):
    """Handle GitHub App installation events."""
    action = payload.get("action")
    installation = payload.get("installation", {})
    account = installation.get("account", {})

    logger.info(f"Installation event: {action} for {account.get('login')}")

    # Store installation metadata
    await publish_event(EventType.CONNECTOR_SYNC_STARTED, {
        "source": "github",
        "event": "installation",
        "action": action,
        "installation_id": installation.get("id"),
        "account": account.get("login"),
        "account_type": account.get("type"),
        "delivery_id": delivery_id,
    })


async def _handle_repository(payload: Dict, delivery_id: str):
    """Handle repository events — created, deleted, renamed."""
    action = payload.get("action")
    repo = payload.get("repository", {})

    await publish_event(EventType.CONNECTOR_SYNC_STARTED, {
        "source": "github",
        "event": "repository",
        "action": action,
        "repo": repo.get("full_name"),
        "delivery_id": delivery_id,
    })


async def _handle_ping(payload: Dict, delivery_id: str):
    """Handle ping event — GitHub sends this on webhook setup."""
    zen = payload.get("zen", "")
    hook_id = payload.get("hook_id")
    logger.info(f"GitHub ping: hook_id={hook_id}, zen='{zen}'")


async def _handle_unknown(payload: Dict, delivery_id: str):
    logger.debug(f"Unhandled GitHub event (delivery: {delivery_id})")
