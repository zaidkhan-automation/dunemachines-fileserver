"""GitHub Action — Push file commit."""
import base64
import logging
from typing import Optional
from app.connectors.github.app_auth import GitHubClient

logger = logging.getLogger(__name__)


async def push_file(
    client: GitHubClient,
    owner: str,
    repo: str,
    path: str,
    content: str,
    message: str,
    branch: str = "main",
    sha: Optional[str] = None,  # Required for updates, not creates
) -> dict:
    """Push a file to GitHub repo (create or update)."""
    encoded = base64.b64encode(content.encode()).decode()

    payload = {
        "message": message,
        "content": encoded,
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha  # Required for file updates

    result = await client.post(f"/repos/{owner}/{repo}/contents/{path}", json=payload)
    logger.info(f"File pushed: {path} to {owner}/{repo}:{branch}")
    return {
        "path": path,
        "sha": result["content"]["sha"],
        "url": result["content"]["html_url"],
        "commit_sha": result["commit"]["sha"],
    }
