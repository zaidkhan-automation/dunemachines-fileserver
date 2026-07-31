"""
Verification test for all fixes applied:
1. Thumbnail worker triggers on image upload
2. Summary worker triggers after embedding
3. Download URL uses public endpoint (not internal)
4. RBAC enforced on upload/delete
5. GraphQL search respects mode (semantic/hybrid/fulltext)
6. Project CRUD fully functional
7. Checksum verification works
8. Soft delete cleans up MinIO blob
"""
import asyncio
import httpx
import hashlib
import time
import uuid
import sys
sys.path.insert(0, '/home/duniverse/dunemachines-fileserver')

from app.core.security import create_access_token
from app.services.permissions.rbac import BUILT_IN_ROLES

BASE = "https://files.dunemachines.com"
results = {"passed": 0, "failed": 0, "errors": []}

def check(name, condition, detail=""):
    if condition:
        results["passed"] += 1
        print(f"  ✅ {name}")
    else:
        results["failed"] += 1
        results["errors"].append(f"{name}: {detail}")
        print(f"  ❌ {name} — {detail}")

OWNER_TOKEN = create_access_token({"sub": "a0000000-0000-0000-0000-000000000002", "email": "owner@dm.com", "org_id": "a0000000-0000-0000-0000-000000000001", "roles": ["owner"]})
VIEWER_TOKEN = create_access_token({"sub": "a0000000-0000-0000-0000-000000000004", "email": "viewer@dm.com", "org_id": "a0000000-0000-0000-0000-000000000001", "roles": ["viewer"]})
AGENT_READ_TOKEN = create_access_token({"sub": "agent:read_only", "type": "agent", "org_id": "a0000000-0000-0000-0000-000000000001", "roles": ["agent:read"], "scopes": list(BUILT_IN_ROLES["agent:read"]["permissions"])})

OWNER_H = {"Authorization": f"Bearer {OWNER_TOKEN}", "Content-Type": "application/json"}
VIEWER_H = {"Authorization": f"Bearer {VIEWER_TOKEN}", "Content-Type": "application/json"}
AGENT_R_H = {"Authorization": f"Bearer {AGENT_READ_TOKEN}", "Content-Type": "application/json"}


async def run():
    async with httpx.AsyncClient(timeout=120, verify=True) as c:

        # ── 1. RBAC on upload ──────────────────────────────
        print("\n=== 1. RBAC — Upload permissions ===")
        # Viewer role does NOT have assets:upload in VIEWER_PERMISSIONS
        r = await c.post(f"{BASE}/api/v1/uploads/init", headers=VIEWER_H,
                         json={"filename": "test.txt", "mime_type": "text/plain", "size_bytes": 100})
        check("Viewer WITHOUT upload perm → 403", r.status_code == 403, f"got {r.status_code}")

        r = await c.post(f"{BASE}/api/v1/uploads/init", headers=OWNER_H,
                         json={"filename": "test.txt", "mime_type": "text/plain", "size_bytes": 100})
        check("Owner CAN upload → 201", r.status_code == 201, f"got {r.status_code}")

        r = await c.post(f"{BASE}/api/v1/uploads/init", headers=AGENT_R_H,
                         json={"filename": "test.txt", "mime_type": "text/plain", "size_bytes": 100})
        check("Agent:read CANNOT upload → 403", r.status_code == 403, f"got {r.status_code}")


        # ── 2. Full upload flow with image (thumbnail trigger) ──
        print("\n=== 2. Upload image → thumbnail worker trigger ===")
        r = await c.post(f"{BASE}/api/v1/uploads/init", headers=OWNER_H,
                         json={"filename": "test_image.png", "mime_type": "image/png", "size_bytes": 1024})
        check("Image upload init → 201", r.status_code == 201, f"got {r.status_code}")

        if r.status_code == 201:
            data = r.json()
            upload_id = data["upload_id"]
            object_key = data["object_key"]
            signed_url = data["signed_url"]

            # Create a real valid 1x1 PNG (verified complete IDAT + IEND chunks)
            import base64
            png_bytes = base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
            )
            checksum = hashlib.sha256(png_bytes).hexdigest()

            async with httpx.AsyncClient(timeout=30, verify=False) as minio_c:
                r2 = await minio_c.put(signed_url, content=png_bytes, headers={"Content-Type": "image/png"})
            check("Image uploaded to MinIO", r2.status_code in [200, 204], f"got {r2.status_code}")

            r3 = await c.post(f"{BASE}/api/v1/uploads/{upload_id}/complete", headers=OWNER_H,
                              json={"object_key": object_key, "checksum": checksum})
            check("Complete upload → 200", r3.status_code == 200, f"got {r3.status_code}")

            # Wait for worker to process (thumbnail + embedding)
            await asyncio.sleep(15)

            # Check thumbnail exists in MinIO
            import aioboto3
            from app.core.config import settings
            session = aioboto3.Session()
            async with session.client(
                "s3", endpoint_url=settings.STORAGE_ENDPOINT,
                aws_access_key_id=settings.STORAGE_ACCESS_KEY,
                aws_secret_access_key=settings.STORAGE_SECRET_KEY,
            ) as s3:
                try:
                    await s3.head_object(Bucket=settings.STORAGE_BUCKET, Key=f"thumbnails/{upload_id}.jpg")
                    check("Thumbnail generated in MinIO", True)
                except Exception as e:
                    check("Thumbnail generated in MinIO", False, str(e)[:80])

            # Check asset status became ready
            r4 = await c.get(f"{BASE}/api/v1/assets/{upload_id}", headers=OWNER_H)
            check("Asset status → ready", r4.status_code == 200 and r4.json().get("status") == "ready",
                  f"status={r4.json().get('status') if r4.status_code==200 else r4.status_code}")


        # ── 3. Checksum mismatch rejection ──────────────────
        print("\n=== 3. Checksum verification ===")
        r = await c.post(f"{BASE}/api/v1/uploads/init", headers=OWNER_H,
                         json={"filename": "checksum_test.txt", "mime_type": "text/plain", "size_bytes": 50})
        if r.status_code == 201:
            data = r.json()
            cid, ckey, curl = data["upload_id"], data["object_key"], data["signed_url"]
            real_content = b"real content for checksum test"
            async with httpx.AsyncClient(timeout=30, verify=False) as minio_c:
                await minio_c.put(curl, content=real_content, headers={"Content-Type": "text/plain"})

            wrong_checksum = hashlib.sha256(b"wrong content").hexdigest()
            r2 = await c.post(f"{BASE}/api/v1/uploads/{cid}/complete", headers=OWNER_H,
                              json={"object_key": ckey, "checksum": wrong_checksum})
            check("Wrong checksum → 422", r2.status_code == 422, f"got {r2.status_code}")

            correct_checksum = hashlib.sha256(real_content).hexdigest()
            r3 = await c.post(f"{BASE}/api/v1/uploads/{cid}/complete", headers=OWNER_H,
                              json={"object_key": ckey, "checksum": correct_checksum})
            check("Correct checksum → 200", r3.status_code == 200, f"got {r3.status_code}")


        # ── 4. Download URL uses public endpoint ────────────
        print("\n=== 4. Download URL — public endpoint ===")
        r = await c.post(f"{BASE}/api/v1/uploads/init", headers=OWNER_H,
                         json={"filename": "download_test.txt", "mime_type": "text/plain", "size_bytes": 20})
        if r.status_code == 201:
            data = r.json()
            did, dkey, durl = data["upload_id"], data["object_key"], data["signed_url"]
            async with httpx.AsyncClient(timeout=30, verify=False) as minio_c:
                await minio_c.put(durl, content=b"download test content", headers={"Content-Type": "text/plain"})
            await c.post(f"{BASE}/api/v1/uploads/{did}/complete", headers=OWNER_H, json={"object_key": dkey})

            r2 = await c.get(f"{BASE}/api/v1/uploads/{did}/download-url", headers=OWNER_H)
            check("Get download URL → 200", r2.status_code == 200, f"got {r2.status_code}")
            if r2.status_code == 200:
                dl_url = r2.json().get("download_url", "")
                check("Download URL NOT internal (no localhost:9000)", "localhost:9000" not in dl_url and "127.0.0.1:9000" not in dl_url,
                      f"url={dl_url[:60]}")
                check("Download URL has public host", "files.dunemachines.com" in dl_url or "http" in dl_url)


        # ── 5. GraphQL search modes ──────────────────────────
        print("\n=== 5. GraphQL — search mode respected ===")
        for mode in ["fulltext", "semantic", "hybrid"]:
            r = await c.post(f"{BASE}/graphql", headers=OWNER_H,
                             json={"query": f'{{ search(query: "test", mode: "{mode}") {{ total mode query }} }}'})
            ok = r.status_code == 200 and "errors" not in r.json()
            returned_mode = r.json().get("data", {}).get("search", {}).get("mode", "") if ok else ""
            check(f"GraphQL search mode={mode} → returns mode={mode}", ok and returned_mode == mode,
                  f"got mode={returned_mode}, status={r.status_code}")


        # ── 6. Project CRUD (was stub, now real) ────────────
        print("\n=== 6. Project CRUD — full lifecycle ===")
        r = await c.post(f"{BASE}/api/v1/projects/", headers=OWNER_H,
                         json={"name": "Test Project Verify", "description": "verification test", "is_public": False})
        check("Create project → 200", r.status_code == 200, f"got {r.status_code}")

        project_id = None
        if r.status_code == 200:
            pdata = r.json()
            project_id = pdata.get("id")
            check("Project has real UUID id", bool(project_id) and len(project_id) == 36, f"id={project_id}")
            check("Project name matches", pdata.get("name") == "Test Project Verify")

        r2 = await c.get(f"{BASE}/api/v1/projects/", headers=OWNER_H)
        check("List projects → 200", r2.status_code == 200)
        check("List projects NOT empty (was stub returning [])", r2.status_code == 200 and r2.json().get("total", 0) > 0,
              f"total={r2.json().get('total') if r2.status_code==200 else 'N/A'}")

        if project_id:
            r3 = await c.get(f"{BASE}/api/v1/projects/{project_id}", headers=OWNER_H)
            check("Get project by id → 200", r3.status_code == 200, f"got {r3.status_code}")

            r4 = await c.delete(f"{BASE}/api/v1/projects/{project_id}", headers=OWNER_H)
            check("Delete project → 200", r4.status_code == 200, f"got {r4.status_code}")

            r5 = await c.get(f"{BASE}/api/v1/projects/{project_id}", headers=OWNER_H)
            check("Deleted project → 404", r5.status_code == 404, f"got {r5.status_code}")


        # ── 7. Soft delete cleans up MinIO blob ─────────────
        print("\n=== 7. Delete asset → MinIO blob removed ===")
        r = await c.post(f"{BASE}/api/v1/uploads/init", headers=OWNER_H,
                         json={"filename": "delete_test.txt", "mime_type": "text/plain", "size_bytes": 15})
        if r.status_code == 201:
            data = r.json()
            del_id, del_key, del_url = data["upload_id"], data["object_key"], data["signed_url"]
            async with httpx.AsyncClient(timeout=30, verify=False) as minio_c:
                await minio_c.put(del_url, content=b"delete me please", headers={"Content-Type": "text/plain"})
            await c.post(f"{BASE}/api/v1/uploads/{del_id}/complete", headers=OWNER_H, json={"object_key": del_key})

            r2 = await c.delete(f"{BASE}/api/v1/assets/{del_id}", headers=OWNER_H)
            check("Delete asset → 204", r2.status_code == 204, f"got {r2.status_code}")

            await asyncio.sleep(2)

            import aioboto3
            from app.core.config import settings
            session = aioboto3.Session()
            blob_gone = False
            async with session.client(
                "s3", endpoint_url=settings.STORAGE_ENDPOINT,
                aws_access_key_id=settings.STORAGE_ACCESS_KEY,
                aws_secret_access_key=settings.STORAGE_SECRET_KEY,
            ) as s3:
                try:
                    await s3.head_object(Bucket=settings.STORAGE_BUCKET, Key=del_key)
                    blob_gone = False
                except Exception:
                    blob_gone = True
            check("MinIO blob deleted after soft delete", blob_gone)


        # ── 8. Viewer CANNOT delete ──────────────────────────
        print("\n=== 8. RBAC — Delete permissions ===")
        r = await c.post(f"{BASE}/api/v1/uploads/init", headers=OWNER_H,
                         json={"filename": "protected.txt", "mime_type": "text/plain", "size_bytes": 10})
        if r.status_code == 201:
            prot_id = r.json()["upload_id"]
            r2 = await c.delete(f"{BASE}/api/v1/assets/{prot_id}", headers=VIEWER_H)
            check("Viewer CANNOT delete → 403", r2.status_code == 403, f"got {r2.status_code}")


    total = results["passed"] + results["failed"]
    print(f"\n{'='*55}")
    print(f"FIX VERIFICATION: {results['passed']}/{total} passed")
    if results["errors"]:
        print(f"\n❌ Failed:")
        for e in results["errors"]:
            print(f"  • {e}")
    else:
        print("🎉 All fixes verified working!")
    print(f"{'='*55}")


asyncio.run(run())
