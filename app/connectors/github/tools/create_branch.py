"""GitHub Action — Create Branch."""
import logging
from app.connectors.github.app_auth import GitHubClient

logger = logging.getLogger(__name__)


async def create_branch(
    client: GitHubClient,
    owner: str,
    repo: str,
    branch_name: str,
    from_branch: str = "main",
) -> dict:
    """Create a new branch from existing branch."""
    # Get SHA of source branch
    ref_data = await client.get(f"/repos/{owner}/{repo}/git/ref/heads/{from_branch}")
    sha = ref_data["object"]["sha"]

    # Create new branch
    result = await client.post(f"/repos/{owner}/{repo}/git/refs", json={
        "ref": f"refs/heads/{branch_name}",
        "sha": sha,
    })
    logger.info(f"Branch '{branch_name}' created in {owner}/{repo}")
    return {
        "branch": branch_name,
        "sha": sha,
        "ref": result.get("ref", ""),
    }
