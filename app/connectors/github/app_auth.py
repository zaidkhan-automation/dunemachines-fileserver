"""
GitHub App Authentication
NEVER stores long-lived tokens.
Flow:
  GitHub App private key → JWT → Exchange for installation token → Use → Expire
"""
import time
import logging
import httpx
from typing import Optional, Dict, Any
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


class GitHubAppAuth:
    def __init__(self, app_id: str, private_key_pem: str):
        self.app_id = app_id
        self.private_key_pem = private_key_pem

    def _generate_jwt(self) -> str:
        """
        Generate short-lived JWT signed with GitHub App private key.
        Valid for max 10 minutes per GitHub specs.
        """
        try:
            import jwt as pyjwt
            now = int(time.time())
            payload = {
                "iat": now - 60,       # Issued 60s ago (clock drift)
                "exp": now + (9 * 60), # Expires in 9 minutes
                "iss": self.app_id,
            }
            token = pyjwt.encode(payload, self.private_key_pem, algorithm="RS256")
            return token
        except Exception as e:
            logger.error(f"JWT generation failed: {e}")
            raise

    async def get_installation_token(self, installation_id: str) -> Dict[str, Any]:
        """
        Exchange JWT for short-lived installation access token.
        Token expires in ~1 hour — never stored permanently.
        Returns: {token, expires_at, permissions, repository_selection}
        """
        jwt_token = self._generate_jwt()
        url = f"{GITHUB_API}/app/installations/{installation_id}/access_tokens"

        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {jwt_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                }
            )
            r.raise_for_status()
            data = r.json()

        logger.info(f"Installation token obtained for installation {installation_id}, expires: {data.get('expires_at')}")
        return {
            "token": data["token"],
            "expires_at": data["expires_at"],
            "permissions": data.get("permissions", {}),
            "repository_selection": data.get("repository_selection", "all"),
        }

    async def get_installation_client(self, installation_id: str) -> "GitHubClient":
        """Get an authenticated client for an installation."""
        token_data = await self.get_installation_token(installation_id)
        return GitHubClient(token=token_data["token"], expires_at=token_data["expires_at"])

    async def list_installations(self) -> list:
        """List all installations of this GitHub App."""
        jwt_token = self._generate_jwt()
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{GITHUB_API}/app/installations",
                headers={
                    "Authorization": f"Bearer {jwt_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                }
            )
            r.raise_for_status()
            return r.json()

    async def verify_webhook_signature(self, payload: bytes, signature: str, secret: str) -> bool:
        """Verify GitHub webhook signature — HMAC-SHA256."""
        import hmac
        import hashlib
        expected = "sha256=" + hmac.new(
            secret.encode(), payload, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)


class GitHubClient:
    """
    Temporary GitHub API client using installation token.
    Token expires in ~1 hour — never persisted.
    """
    def __init__(self, token: str, expires_at: str = None):
        self.token = token
        self.expires_at = expires_at
        self.headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def is_expired(self) -> bool:
        if not self.expires_at:
            return False
        exp = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        return datetime.now(exp.tzinfo) >= exp

    async def get(self, path: str, params: dict = None) -> Any:
        if self.is_expired():
            raise Exception("Installation token expired — refresh required")
        url = f"{GITHUB_API}{path}" if not path.startswith("http") else path
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url, headers=self.headers, params=params)
            r.raise_for_status()
            return r.json()

    async def post(self, path: str, json: dict = None) -> Any:
        if self.is_expired():
            raise Exception("Installation token expired — refresh required")
        url = f"{GITHUB_API}{path}" if not path.startswith("http") else path
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(url, headers=self.headers, json=json)
            r.raise_for_status()
            return r.json()

    async def patch(self, path: str, json: dict = None) -> Any:
        url = f"{GITHUB_API}{path}" if not path.startswith("http") else path
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.patch(url, headers=self.headers, json=json)
            r.raise_for_status()
            return r.json()
