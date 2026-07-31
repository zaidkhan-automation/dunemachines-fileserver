"""
GitHub Action — Create Pull Request
ACTION LAYER ONLY — writes to GitHub.
"""
import logging
from typing import Optional
from app.connectors.github.app_auth import GitHubClient

logger = logging.getLogger(__name__)


async def create_pull_request(
    client: GitHubClient,
    owner: str,
    repo: str,
    title: str,
    body: str,
    head: str,
    base: str,
    draft: bool = False,
) -> dict:
    """Create a PR on GitHub."""
    result = await client.post(f"/repos/{owner}/{repo}/pulls", json={
        "title": title,
        "body": body,
        "head": head,
        "base": base,
        "draft": draft,
    })
    logger.info(f"PR created: #{result['number']} in {owner}/{repo}")
    return {
        "pr_number": result["number"],
        "pr_url": result["html_url"],
        "state": result["state"],
        "title": result["title"],
    }
