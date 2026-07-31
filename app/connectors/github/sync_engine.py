"""
GitHub Sync Engine
Reads GitHub state into our platform as Assets.
SYNC LAYER ONLY — no write operations to GitHub here.
Write operations are in tools/ layer.
"""
import logging
from typing import Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from app.connectors.github.app_auth import GitHubClient
from app.models.asset import AssetType, AssetStatus, SourceType
from app.repositories.asset_repo import asset_repo

logger = logging.getLogger(__name__)


class GitHubSyncEngine:
    """
    Sync engine — reads GitHub → creates/updates Assets.
    Never writes back to GitHub.
    """

    def __init__(self, client: GitHubClient, org_id: str, user_id: str):
        import uuid as _uuid
        try:
            _uuid.UUID(str(user_id))
        except (ValueError, AttributeError):
            user_id = "00000000-0000-0000-0000-000000000000"
        self.client = client
        self.org_id = org_id
        self.user_id = user_id

    async def sync_repo(self, owner: str, repo_name: str, db: AsyncSession) -> Dict:
        """Full repo sync — metadata + files + issues + PRs."""
        results = {"repo_id": None, "files": 0, "issues": 0, "prs": 0, "commits": 0, "errors": []}

        try:
            # 1. Repo metadata
            repo_data = await self.client.get(f"/repos/{owner}/{repo_name}")
            repo_asset = await self._upsert_repo_asset(repo_data, db)
            results["repo_id"] = str(repo_asset.id)

            # 2. Files
            results["files"] = await self._sync_files(owner, repo_name, str(repo_asset.id), db)

            # 3. Issues
            results["issues"] = await self._sync_issues(owner, repo_name, str(repo_asset.id), db)

            # 4. PRs
            results["prs"] = await self._sync_prs(owner, repo_name, str(repo_asset.id), db)

            logger.info(f"Sync complete for {owner}/{repo_name}: {results}")

        except Exception as e:
            logger.error(f"Sync failed for {owner}/{repo_name}: {e}")
            results["errors"].append(str(e))

        return results

    async def _upsert_repo_asset(self, repo_data: Dict, db: AsyncSession):
        """Create or update repo asset."""
        return await asset_repo.create(db, {
            "organization_id": self.org_id,
            "created_by": self.user_id,
            "name": repo_data["full_name"],
            "asset_type": AssetType.REPO,
            "source_type": SourceType.GITHUB,
            "status": AssetStatus.READY,
            "external_id": str(repo_data["id"]),
            "external_url": repo_data["html_url"],
            "summary": repo_data.get("description") or f"GitHub: {repo_data['full_name']}",
            "tags": repo_data.get("topics", []),
            "extra_data": {
                "owner": repo_data["owner"]["login"],
                "repo": repo_data["name"],
                "full_name": repo_data["full_name"],
                "description": repo_data.get("description", ""),
                "stars": repo_data.get("stargazers_count", 0),
                "forks": repo_data.get("forks_count", 0),
                "language": repo_data.get("language", ""),
                "default_branch": repo_data.get("default_branch", "main"),
                "private": repo_data.get("private", False),
                "topics": repo_data.get("topics", []),
                "clone_url": repo_data.get("clone_url", ""),
                "open_issues_count": repo_data.get("open_issues_count", 0),
                "size_kb": repo_data.get("size", 0),
                "created_at": repo_data.get("created_at", ""),
                "updated_at": repo_data.get("updated_at", ""),
            },
        })

    async def _sync_files(self, owner: str, repo: str, parent_id: str, db: AsyncSession) -> int:
        """Sync root-level files as CODE assets."""
        count = 0
        try:
            files = await self.client.get(f"/repos/{owner}/{repo}/contents")
            if not isinstance(files, list):
                return 0
            for f in files[:30]:
                if f["type"] != "file":
                    continue
                try:
                    await asset_repo.create(db, {
                        "organization_id": self.org_id,
                        "created_by": self.user_id,
                        "parent_id": parent_id,
                        "name": f["name"],
                        "asset_type": AssetType.CODE,
                        "source_type": SourceType.GITHUB,
                        "status": AssetStatus.READY,
                        "external_id": f["sha"],
                        "external_url": f.get("html_url", ""),
                        "size_bytes": f.get("size", 0),
                        "extra_data": {
                            "path": f["path"],
                            "sha": f["sha"],
                            "download_url": f.get("download_url", ""),
                            "repo": f"{owner}/{repo}",
                        },
                    })
                    count += 1
                except Exception as e:
                    logger.warning(f"File sync failed {f['name']}: {e}")
        except Exception as e:
            logger.warning(f"Files listing failed: {e}")
        return count

    async def _sync_issues(self, owner: str, repo: str, parent_id: str, db: AsyncSession) -> int:
        """Sync issues as ISSUE assets."""
        count = 0
        try:
            issues = await self.client.get(
                f"/repos/{owner}/{repo}/issues",
                params={"state": "all", "per_page": 30}
            )
            for issue in issues:
                if "pull_request" in issue:
                    continue  # Skip PRs from issues list
                try:
                    await asset_repo.create(db, {
                        "organization_id": self.org_id,
                        "created_by": self.user_id,
                        "parent_id": parent_id,
                        "name": f"#{issue['number']}: {issue['title'][:100]}",
                        "asset_type": AssetType.ISSUE,
                        "source_type": SourceType.GITHUB,
                        "status": AssetStatus.READY,
                        "external_id": str(issue["id"]),
                        "external_url": issue["html_url"],
                        "summary": (issue.get("body") or "")[:500],
                        "tags": [l["name"] for l in issue.get("labels", [])],
                        "extra_data": {
                            "number": issue["number"],
                            "state": issue["state"],
                            "author": issue["user"]["login"],
                            "labels": [l["name"] for l in issue.get("labels", [])],
                            "comments": issue.get("comments", 0),
                            "repo": f"{owner}/{repo}",
                            "created_at": issue.get("created_at", ""),
                            "closed_at": issue.get("closed_at"),
                        },
                    })
                    count += 1
                except Exception as e:
                    logger.warning(f"Issue sync failed #{issue['number']}: {e}")
        except Exception as e:
            logger.warning(f"Issues listing failed: {e}")
        return count

    async def _sync_prs(self, owner: str, repo: str, parent_id: str, db: AsyncSession) -> int:
        """Sync pull requests as PR assets."""
        count = 0
        try:
            prs = await self.client.get(
                f"/repos/{owner}/{repo}/pulls",
                params={"state": "all", "per_page": 20}
            )
            for pr in prs:
                try:
                    await asset_repo.create(db, {
                        "organization_id": self.org_id,
                        "created_by": self.user_id,
                        "parent_id": parent_id,
                        "name": f"PR #{pr['number']}: {pr['title'][:100]}",
                        "asset_type": AssetType.PR,
                        "source_type": SourceType.GITHUB,
                        "status": AssetStatus.READY,
                        "external_id": str(pr["id"]),
                        "external_url": pr["html_url"],
                        "summary": (pr.get("body") or "")[:500],
                        "extra_data": {
                            "number": pr["number"],
                            "state": pr["state"],
                            "author": pr["user"]["login"],
                            "base_branch": pr["base"]["ref"],
                            "head_branch": pr["head"]["ref"],
                            "merged": pr.get("merged", False),
                            "draft": pr.get("draft", False),
                            "repo": f"{owner}/{repo}",
                            "created_at": pr.get("created_at", ""),
                            "merged_at": pr.get("merged_at"),
                        },
                    })
                    count += 1
                except Exception as e:
                    logger.warning(f"PR sync failed #{pr['number']}: {e}")
        except Exception as e:
            logger.warning(f"PRs listing failed: {e}")
        return count
