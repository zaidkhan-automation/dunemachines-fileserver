"""GitHub Action — Merge Pull Request."""
import logging
from app.connectors.github.app_auth import GitHubClient

logger = logging.getLogger(__name__)


async def merge_pull_request(
    client: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    commit_title: str = "",
    merge_method: str = "merge",  # merge, squash, rebase
) -> dict:
    """Merge a PR on GitHub."""
    result = await client.post(f"/repos/{owner}/{repo}/pulls/{pr_number}/merge", json={
        "commit_title": commit_title,
        "merge_method": merge_method,
    })
    logger.info(f"PR #{pr_number} merged in {owner}/{repo}")
    return {"merged": result.get("merged", False), "message": result.get("message", "")}
