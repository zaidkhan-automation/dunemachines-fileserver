"""GitHub Action — Comment on PR or Issue."""
import logging
from app.connectors.github.app_auth import GitHubClient

logger = logging.getLogger(__name__)


async def comment_on_issue(
    client: GitHubClient,
    owner: str,
    repo: str,
    issue_number: int,
    body: str,
) -> dict:
    """Add comment to PR or issue (both use same API)."""
    result = await client.post(
        f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
        json={"body": body}
    )
    logger.info(f"Comment added to #{issue_number} in {owner}/{repo}")
    return {"comment_id": result["id"], "url": result["html_url"]}


async def add_pr_review(
    client: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    body: str,
    event: str = "COMMENT",  # APPROVE, REQUEST_CHANGES, COMMENT
) -> dict:
    """Add review to PR."""
    result = await client.post(
        f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
        json={"body": body, "event": event}
    )
    return {"review_id": result["id"], "state": result["state"]}
