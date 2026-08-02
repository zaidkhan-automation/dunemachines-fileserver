"""
Connector endpoints — GitHub, GDrive sync.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.security import get_current_user
from app.services.permissions.rbac import require_permission

router = APIRouter(prefix="/connectors", tags=["connectors"])


# ── GitHub ────────────────────────────────────────────────────────

class GitHubSyncRequest(BaseModel):
    access_token: str = Field(..., min_length=1, description="GitHub personal access token")
    owner: str = Field(..., min_length=1, description="GitHub username or org")
    repo: str = Field(..., min_length=1, description="Repository name")


class GitHubListReposRequest(BaseModel):
    access_token: str = Field(..., min_length=1)
    org: Optional[str] = None  # If set, list org repos instead of user repos


@router.post("/github/sync", status_code=status.HTTP_200_OK)
async def sync_github_repo(
    req: GitHubSyncRequest,
    db: AsyncSession = Depends(get_db),
    user: Dict = Depends(require_permission("connectors", "sync")),
):
    """
    Sync a GitHub repository as assets.
    Creates: REPO + CODE files + ISSUES + PRs as assets.
    """
    from app.connectors.github.github_connector import GitHubConnector

    connector = GitHubConnector(
        access_token=req.access_token,
        org_id=user["org_id"],
        user_id=user["user_id"],
    )

    # Verify token works
    try:
        gh_user = await connector.get_user()
        logger_name = gh_user.get("login", "unknown")
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"GitHub auth failed: {str(e)}")

    # Run sync
    try:
        result = await connector.sync_repo_as_asset(req.owner, req.repo, db)
        return {
            "success": True,
            "github_user": logger_name,
            "synced": result,
            "message": f"Repo {req.owner}/{req.repo} synced successfully",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sync failed: {str(e)}")


@router.post("/github/repos")
async def list_github_repos(
    req: GitHubListReposRequest,
    user: Dict = Depends(get_current_user),
):
    """List available GitHub repositories."""
    from app.connectors.github.github_connector import GitHubConnector

    connector = GitHubConnector(
        access_token=req.access_token,
        org_id=user["org_id"],
        user_id=user["user_id"],
    )

    try:
        if req.org:
            repos = await connector.list_org_repos(req.org)
        else:
            repos = await connector.list_repos()

        return {
            "repos": [
                {
                    "full_name": r["full_name"],
                    "description": r.get("description", ""),
                    "language": r.get("language", ""),
                    "stars": r.get("stargazers_count", 0),
                    "private": r.get("private", False),
                    "updated_at": r.get("updated_at", ""),
                }
                for r in repos
            ],
            "total": len(repos),
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/github/status")
async def github_connector_status(user: Dict = Depends(get_current_user)):
    """Get synced GitHub assets count."""
    return {
        "connector": "github",
        "status": "active",
        "supported_asset_types": ["repo", "code", "issue", "pr"],
    }


# ── GitHub Actions (Action Layer) ────────────────────────────────

class GitHubInstallationRequest(BaseModel):
    installation_id: str = Field(default="133640523")
    owner: str
    repo: str


class GitHubPRRequest(GitHubInstallationRequest):
    title: str
    body: str = ""
    head: str
    base: str = "main"


class GitHubBranchRequest(GitHubInstallationRequest):
    branch_name: str
    from_branch: str = "main"


class GitHubFileRequest(GitHubInstallationRequest):
    path: str
    content: str
    message: str
    branch: str = "main"
    sha: Optional[str] = None


class GitHubCommentRequest(GitHubInstallationRequest):
    issue_number: int
    body: str


async def _get_github_client(installation_id: str):
    """Get GitHub client using App auth."""
    import os
    with open("/etc/dunemachines/github_private_key.pem") as f:
        private_key = f.read()
    from app.connectors.github.app_auth import GitHubAppAuth
    auth = GitHubAppAuth(app_id="3765696", private_key_pem=private_key)
    return await auth.get_installation_client(installation_id)


@router.post("/github/pr/create")
async def create_github_pr(req: GitHubPRRequest, user: Dict = Depends(require_permission("connectors", "pr:create"))):
    """Create a pull request on GitHub."""
    try:
        client = await _get_github_client(req.installation_id)
        from app.connectors.github.tools.create_pr import create_pull_request
        result = await create_pull_request(client, req.owner, req.repo, req.title, req.body, req.head, req.base)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/github/branch/create")
async def create_github_branch(req: GitHubBranchRequest, user: Dict = Depends(require_permission("connectors", "branch:create"))):
    """Create a branch on GitHub."""
    try:
        client = await _get_github_client(req.installation_id)
        from app.connectors.github.tools.create_branch import create_branch
        result = await create_branch(client, req.owner, req.repo, req.branch_name, req.from_branch)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/github/file/push")
async def push_github_file(req: GitHubFileRequest, user: Dict = Depends(require_permission("connectors", "commit"))):
    """Push a file to GitHub repo."""
    try:
        client = await _get_github_client(req.installation_id)
        from app.connectors.github.tools.push_commit import push_file
        result = await push_file(client, req.owner, req.repo, req.path, req.content, req.message, req.branch, req.sha)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/github/comment")
async def github_comment(req: GitHubCommentRequest, user: Dict = Depends(require_permission("connectors", "pr:comment"))):
    """Comment on a GitHub issue or PR."""
    try:
        client = await _get_github_client(req.installation_id)
        from app.connectors.github.tools.comment_pr import comment_on_issue
        result = await comment_on_issue(client, req.owner, req.repo, req.issue_number, req.body)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/github/sync/installation")
async def sync_via_installation(
    installation_id: str,
    owner: str,
    repo: str,
    db=Depends(get_db),
    user: Dict = Depends(require_permission("connectors", "sync")),
):
    """Sync repo using GitHub App installation (no PAT needed)."""
    try:
        client = await _get_github_client(installation_id)
        from app.connectors.github.sync_engine import GitHubSyncEngine
        engine = GitHubSyncEngine(client=client, org_id=user["org_id"], user_id=user["user_id"])
        result = await engine.sync_repo(owner, repo, db)
        return {"success": True, "synced": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/github/installation/repos")
async def list_installation_repos(
    installation_id: str = "133640523",
    user: Dict = Depends(get_current_user),
):
    """List repos accessible via GitHub App installation."""
    try:
        client = await _get_github_client(installation_id)
        data = await client.get("/installation/repositories", params={"per_page": 100})
        repos = data.get("repositories", [])
        return {
            "repos": [
                {
                    "full_name": r["full_name"],
                    "description": r.get("description", ""),
                    "language": r.get("language", ""),
                    "stars": r.get("stargazers_count", 0),
                    "private": r.get("private", False),
                    "updated_at": r.get("updated_at", ""),
                    "default_branch": r.get("default_branch", "main"),
                }
                for r in repos
            ],
            "total": len(repos),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
