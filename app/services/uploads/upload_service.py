"""
Upload service — signed URL based uploads.

Flow:
1. Client calls POST /uploads/init → gets signed URL + upload_id
2. Client uploads directly to MinIO using signed URL  
3. Client calls POST /uploads/{upload_id}/complete
4. Workers process the asset (OCR, embed, thumbnail)
"""
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any
from app.core.config import settings
from app.core.s3_client import get_s3_client
from app.models.asset import AssetType, AssetStatus

# Defensive bounds on presigned-URL lifetime — no caller passes a value
# outside this range today (both call sites use the 3600s default), but
# clamping here means a future caller can never accidentally mint a
# near-infinite or near-zero-lifetime signed URL.
_MIN_EXPIRY_SECONDS = 300      # 5 minutes
_MAX_EXPIRY_SECONDS = 86400    # 24 hours


def _clamp_expiry(expires_in: int) -> int:
    return max(_MIN_EXPIRY_SECONDS, min(expires_in, _MAX_EXPIRY_SECONDS))


class UploadService:
    def __init__(self):
        self.bucket = settings.STORAGE_BUCKET
        self.endpoint = settings.STORAGE_ENDPOINT

    def _get_object_key(self, org_id: str, asset_id: str, filename: str) -> str:
        return f"orgs/{org_id}/assets/{asset_id}/{filename}"

    async def init_upload(
        self,
        org_id: str,
        project_id: Optional[str],
        user_id: str,
        filename: str,
        mime_type: str,
        size_bytes: int,
        asset_type: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        if not asset_type:
            asset_type = self._detect_asset_type(mime_type, filename)

        asset_id = str(uuid.uuid4())
        object_key = self._get_object_key(org_id, asset_id, filename)

        signed_url = await self._generate_presigned_put(
            object_key=object_key,
            mime_type=mime_type,
            expires_in=3600,
        )

        return {
            "upload_id": asset_id,
            "signed_url": signed_url,
            "object_key": object_key,
            "expires_at": (datetime.utcnow() + timedelta(hours=1)).isoformat(),
            "asset": {
                "id": asset_id,
                "name": filename,
                "asset_type": asset_type,
                "status": AssetStatus.PENDING,
                "mime_type": mime_type,
                "size_bytes": size_bytes,
            }
        }

    async def complete_upload(
        self, asset_id: str, org_id: str, filename: str, checksum: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Security fix (F-02, remediation audit): the object_key used to
        bind this asset's blob_ref is now ALWAYS re-derived server-side
        from (org_id, asset_id, filename) via _get_object_key — the same
        deterministic function init_upload used to build the original
        presigned PUT URL — rather than trusted from the client's request
        body. Previously the REST handler passed req.object_key (a raw
        client-supplied string) straight through to _verify_object_exists
        and then into blob_ref: a caller who owned ANY asset in ANY org
        could call /complete with a DIFFERENT org's real object_key
        (these leak inherently in presigned download/thumbnail URLs) and
        bind their own asset to that foreign object. Since org_id/asset_id/
        filename here all come from the already org-scoped Asset row
        (asset_repo.get_by_id already filtered by the caller's own org),
        the recomputed key can only ever point at storage the caller's
        own org actually owns — cross-org binding is now structurally
        impossible, not just checked."""
        object_key = self._get_object_key(org_id, asset_id, filename)
        exists = await self._verify_object_exists(object_key)
        if not exists:
            raise ValueError(f"Object not found in storage: {object_key}")
        return {
            "asset_id": asset_id,
            "object_key": object_key,
            "status": AssetStatus.PROCESSING,
            "message": "Upload complete. Processing started.",
        }

    async def generate_presigned_get(self, object_key: str, expires_in: int = 3600, filename: Optional[str] = None) -> str:
        expires_in = _clamp_expiry(expires_in)
        params = {"Bucket": self.bucket, "Key": object_key}
        if filename:
            params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
        s3 = get_s3_client()
        url = await s3.generate_presigned_url("get_object", Params=params, ExpiresIn=expires_in)
        public = settings.STORAGE_PUBLIC_ENDPOINT
        if public and public != self.endpoint:
            url = url.replace(self.endpoint, public)
        return url

    async def _generate_presigned_put(self, object_key: str, mime_type: str, expires_in: int = 3600) -> str:
        expires_in = _clamp_expiry(expires_in)
        s3 = get_s3_client()
        url = await s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": object_key, "ContentType": mime_type},
            ExpiresIn=expires_in,
        )
        # Replace internal endpoint with public endpoint for external clients
        public = settings.STORAGE_PUBLIC_ENDPOINT
        if public and public != self.endpoint:
            url = url.replace(self.endpoint, public)
        return url

    async def _verify_object_exists(self, object_key: str) -> bool:
        try:
            s3 = get_s3_client()
            await s3.head_object(Bucket=self.bucket, Key=object_key)
            return True
        except Exception:
            return False

    def _detect_asset_type(self, mime_type: str, filename: str) -> str:
        mime = mime_type.lower()
        # Parquet has no IANA-registered MIME type, so most callers land on
        # something generic for it — the exact live bug this guards against:
        # dunemachines_backend's own fileserver_sync.py MIME_MAP fell back to
        # "text/plain" for .parquet (not in that map), which used to make it
        # past the mime.startswith("text/") branch below into AssetType.CODE.
        # AssetType.DATASET already exists in the taxonomy for exactly this
        # shape of file but was never reachable from here — check the
        # filename extension directly (this function already receives it,
        # previously unused) rather than depending on every caller sending a
        # correct mime_type for a format with no standard one.
        if Path(filename).suffix.lower() == ".parquet":
            return AssetType.DATASET
        if mime.startswith("image/"): return AssetType.IMAGE
        elif mime.startswith("video/"): return AssetType.VIDEO
        elif mime.startswith("audio/"): return AssetType.AUDIO
        elif mime in ["application/pdf", "application/msword",
                      "application/vnd.openxmlformats-officedocument.wordprocessingml.document"]:
            return AssetType.DOCUMENT
        elif mime in ["application/vnd.ms-excel",
                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "text/csv"]:
            return AssetType.SPREADSHEET
        elif mime.startswith("text/") or mime in ["application/json", "application/xml"]:
            return AssetType.CODE
        else:
            return AssetType.FILE

upload_service = UploadService()
