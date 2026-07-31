"""
GitHub Connector
Syncs GitHub repos, files, issues, PRs as Assets.

Supported asset types from GitHub:
- REPO — repository metadata
- CODE — files from repo
- ISSUE — GitHub issues
- PR — pull requests
"""
import asyncio
import logging
import httpx
from datetime import datetime
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


class GitHubConnector:
    def __init__(self, access_token: str, org_id: str, user_id: str):
        self.token = access_token
        self.org_id = org_id
        self.user_id = user_id
        self.headers = {
            "Authorization": f"token {access_token}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _get(self, url: str, params: dict = None) -> Any:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url, headers=self.headers, params=params)
            r.raise_for_status()
            return r.json()

    async def get_user(self) -> Dict:
        """Get authenticated GitHub user."""
        return await self._get(f"{GITHUB_API}/user")

    async def list_repos(self, per_page: int = 30) -> List[Dict]:
        """List repos for authenticated user."""
        return await self._get(f"{GITHUB_API}/user/repos", params={
            "per_page": per_page,
            "sort": "updated",
            "type": "all",
        })

    async def list_org_repos(self, org: str, per_page: int = 30) -> List[Dict]:
        """List repos for an organization."""
        return await self._get(f"{GITHUB_API}/orgs/{org}/repos", params={
            "per_page": per_page,
            "sort": "updated",
        })

    async def get_repo(self, owner: str, repo: str) -> Dict:
        """Get repo metadata."""
        return await self._get(f"{GITHUB_API}/repos/{owner}/{repo}")

    async def list_repo_files(self, owner: str, repo: str, path: str = "") -> List[Dict]:
        """List files in a repo directory."""
        return await self._get(f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}")

    async def get_file_content(self, owner: str, repo: str, path: str) -> Dict:
        """Get file content from repo."""
        return await self._get(f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}")

    async def list_issues(self, owner: str, repo: str, state: str = "open") -> List[Dict]:
        """List issues for a repo."""
        return await self._get(f"{GITHUB_API}/repos/{owner}/{repo}/issues", params={
            "state": state,
            "per_page": 50,
        })

    async def list_prs(self, owner: str, repo: str, state: str = "open") -> List[Dict]:
        """List pull requests for a repo."""
        return await self._get(f"{GITHUB_API}/repos/{owner}/{repo}/pulls", params={
            "state": state,
            "per_page": 50,
        })

    async def sync_repo_as_asset(self, owner: str, repo_name: str, db) -> Dict:
        """
        Sync a GitHub repo as an Asset.
        Creates: 1 REPO asset + N CODE assets (files) + M ISSUE assets + K PR assets
        """
        from app.repositories.asset_repo import asset_repo
        from app.models.asset import AssetType, AssetStatus, SourceType

        synced = {"repo": None, "files": 0, "issues": 0, "prs": 0, "errors": []}

        try:
            # 1. Sync repo metadata as REPO asset
            repo_data = await self.get_repo(owner, repo_name)

            repo_asset = await asset_repo.create(db, {
                "organization_id": self.org_id,
                "created_by": self.user_id,
                "name": repo_data["full_name"],
                "asset_type": AssetType.REPO,
                "source_type": SourceType.GITHUB,
                "status": AssetStatus.READY,
                "external_id": str(repo_data["id"]),
                "external_url": repo_data["html_url"],
                "extra_data": {
                    "owner": owner,
                    "repo": repo_name,
                    "full_name": repo_data["full_name"],
                    "description": repo_data.get("description", ""),
                    "stars": repo_data.get("stargazers_count", 0),
                    "forks": repo_data.get("forks_count", 0),
                    "language": repo_data.get("language", ""),
                    "default_branch": repo_data.get("default_branch", "main"),
                    "private": repo_data.get("private", False),
                    "topics": repo_data.get("topics", []),
                    "clone_url": repo_data.get("clone_url", ""),
                },
                "tags": repo_data.get("topics", []),
                "summary": repo_data.get("description") or f"GitHub repo: {repo_data['full_name']}",
            })
            synced["repo"] = str(repo_asset.id)
            logger.info(f"Repo asset created: {repo_data['full_name']}")

            # 2. Sync root files as CODE assets
            try:
                files = await self.list_repo_files(owner, repo_name)
                for f in files[:20]:  # Cap at 20 files
                    if f["type"] != "file":
                        continue
                    try:
                        await asset_repo.create(db, {
                            "organization_id": self.org_id,
                            "created_by": self.user_id,
                            "parent_id": str(repo_asset.id),
                            "name": f["name"],
                            "asset_type": AssetType.CODE,
                            "source_type": SourceType.GITHUB,
                            "status": AssetStatus.READY,
                            "external_id": f["sha"],
                            "external_url": f["html_url"],
                            "size_bytes": f.get("size", 0),
                            "extra_data": {
                                "path": f["path"],
                                "sha": f["sha"],
                                "download_url": f.get("download_url", ""),
                                "repo": repo_data["full_name"],
                            },
                        })
                        synced["files"] += 1
                    except Exception as e:
                        synced["errors"].append(f"File {f['name']}: {str(e)[:50]}")
            except Exception as e:
                logger.warning(f"Files sync failed: {e}")

            # 3. Sync issues as ISSUE assets
            try:
                issues = await self.list_issues(owner, repo_name)
                for issue in issues[:20]:
                    try:
                        await asset_repo.create(db, {
                            "organization_id": self.org_id,
                            "created_by": self.user_id,
                            "parent_id": str(repo_asset.id),
                            "name": f"#{issue['number']}: {issue['title'][:100]}",
                            "asset_type": AssetType.ISSUE,
                            "source_type": SourceType.GITHUB,
                            "status": AssetStatus.READY,
                            "external_id": str(issue["id"]),
                            "external_url": issue["html_url"],
                            "summary": issue.get("body", "")[:500] if issue.get("body") else "",
                            "extra_data": {
                                "number": issue["number"],
                                "state": issue["state"],
                                "author": issue["user"]["login"],
                                "labels": [l["name"] for l in issue.get("labels", [])],
                                "repo": repo_data["full_name"],
                            },
                            "tags": [l["name"] for l in issue.get("labels", [])],
                        })
                        synced["issues"] += 1
                    except Exception as e:
                        synced["errors"].append(f"Issue #{issue['number']}: {str(e)[:50]}")
            except Exception as e:
                logger.warning(f"Issues sync failed: {e}")

            # 4. Sync PRs as PR assets
            try:
                prs = await self.list_prs(owner, repo_name)
                for pr in prs[:10]:
                    try:
                        await asset_repo.create(db, {
                            "organization_id": self.org_id,
                            "created_by": self.user_id,
                            "parent_id": str(repo_asset.id),
                            "name": f"PR #{pr['number']}: {pr['title'][:100]}",
                            "asset_type": AssetType.PR,
                            "source_type": SourceType.GITHUB,
                            "status": AssetStatus.READY,
                            "external_id": str(pr["id"]),
                            "external_url": pr["html_url"],
                            "summary": pr.get("body", "")[:500] if pr.get("body") else "",
                            "extra_data": {
                                "number": pr["number"],
                                "state": pr["state"],
                                "author": pr["user"]["login"],
                                "base_branch": pr["base"]["ref"],
                                "head_branch": pr["head"]["ref"],
                                "repo": repo_data["full_name"],
                                "mergeable": pr.get("mergeable"),
                            },
                        })
                        synced["prs"] += 1
                    except Exception as e:
                        synced["errors"].append(f"PR #{pr['number']}: {str(e)[:50]}")
            except Exception as e:
                logger.warning(f"PRs sync failed: {e}")

        except Exception as e:
            logger.error(f"Repo sync failed: {e}")
            synced["errors"].append(str(e))

        logger.info(f"Sync complete: {synced}")
        return synced
