"""
Comprehensive stress test — security, edge cases, load.
"""
import asyncio
import httpx
import json
import time
import uuid
from typing import List

BASE_URL = "http://localhost:8007"

# ── Test tokens ──────────────────────────────────────────────────
import sys
sys.path.insert(0, '/home/duniverse/dunemachines-fileserver')
from app.core.security import create_access_token

VALID_TOKEN = create_access_token({
    "sub": "a0000000-0000-0000-0000-000000000002",
    "email": "ahmed@dunemachines.com",
    "org_id": "a0000000-0000-0000-0000-000000000001",
    "roles": ["admin"],
})

OTHER_ORG_TOKEN = create_access_token({
    "sub": "b0000000-0000-0000-0000-000000000002",
    "email": "attacker@evil.com",
    "org_id": "b0000000-0000-0000-0000-000000000099",
    "roles": ["user"],
})

HEADERS = {"Authorization": f"Bearer {VALID_TOKEN}", "Content-Type": "application/json"}
OTHER_HEADERS = {"Authorization": f"Bearer {OTHER_ORG_TOKEN}", "Content-Type": "application/json"}

results = {"passed": 0, "failed": 0, "errors": []}

def check(name: str, condition: bool, detail: str = ""):
    if condition:
        results["passed"] += 1
        print(f"  ✅ {name}")
    else:
        results["failed"] += 1
        results["errors"].append(f"{name}: {detail}")
        print(f"  ❌ {name} — {detail}")


async def run_tests():
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as client:

        print("\n=== 1. HEALTH & INFO ===")
        r = await client.get("/health")
        check("Health endpoint", r.status_code == 200)
        check("Health returns ok", r.json()["status"] == "ok")

        r = await client.get("/info")
        check("Info endpoint", r.status_code == 200)
        check("Info has endpoints", "endpoints" in r.json())


        print("\n=== 2. AUTH SECURITY ===")
        # No token
        r = await client.get("/api/v1/assets/")
        check("No token → 403", r.status_code == 403)

        # Invalid token
        r = await client.get("/api/v1/assets/", headers={"Authorization": "Bearer invalid.token.here"})
        check("Invalid token → 401", r.status_code == 401)

        # Malformed bearer
        r = await client.get("/api/v1/assets/", headers={"Authorization": "NotBearer abc"})
        check("Malformed auth → 403", r.status_code == 403)

        # Expired token (manually crafted)
        from jose import jwt
        from app.core.config import settings
        from datetime import datetime, timedelta
        expired = jwt.encode({"sub": "test", "exp": datetime.utcnow() - timedelta(hours=1)},
                             settings.JWT_SECRET, algorithm="HS256")
        r = await client.get("/api/v1/assets/", headers={"Authorization": f"Bearer {expired}"})
        check("Expired token → 401", r.status_code == 401)


        print("\n=== 3. UPLOAD VALIDATION ===")
        # Missing required fields
        r = await client.post("/api/v1/uploads/init", headers=HEADERS, json={})
        check("Empty upload body → 422", r.status_code == 422)

        # File too large
        r = await client.post("/api/v1/uploads/init", headers=HEADERS, json={
            "filename": "big.pdf", "mime_type": "application/pdf",
            "size_bytes": 600 * 1024 * 1024  # 600MB over limit
        })
        check("File too large → 422", r.status_code == 422)

        # Zero size
        r = await client.post("/api/v1/uploads/init", headers=HEADERS, json={
            "filename": "empty.pdf", "mime_type": "application/pdf", "size_bytes": 0
        })
        check("Zero size → 422", r.status_code == 422)

        # Empty filename
        r = await client.post("/api/v1/uploads/init", headers=HEADERS, json={
            "filename": "", "mime_type": "application/pdf", "size_bytes": 1024
        })
        check("Empty filename → 422", r.status_code == 422)

        # Valid upload
        r = await client.post("/api/v1/uploads/init", headers=HEADERS, json={
            "filename": "test.pdf", "mime_type": "application/pdf", "size_bytes": 1024
        })
        check("Valid upload init → 201", r.status_code == 201)
        asset_id = r.json().get("upload_id", "")
        check("Upload returns signed URL", "signed_url" in r.json())
        check("Upload returns object_key", "object_key" in r.json())


        print("\n=== 4. TENANT ISOLATION (Security) ===")
        # Other org cannot access first org's assets
        r = await client.get("/api/v1/assets/", headers=OTHER_HEADERS)
        check("Other org gets empty list", r.status_code == 200 and r.json()["total"] == 0)

        # Other org cannot access specific asset
        if asset_id:
            r = await client.get(f"/api/v1/assets/{asset_id}", headers=OTHER_HEADERS)
            check("Other org cannot get asset → 404", r.status_code == 404)

        # Other org cannot delete
        if asset_id:
            r = await client.delete(f"/api/v1/assets/{asset_id}", headers=OTHER_HEADERS)
            check("Other org cannot delete → 404", r.status_code == 404)


        print("\n=== 5. ASSET CRUD ===")
        # Get existing asset
        if asset_id:
            r = await client.get(f"/api/v1/assets/{asset_id}", headers=HEADERS)
            check("Get asset by ID → 200", r.status_code == 200)
            check("Asset has correct fields", all(k in r.json() for k in ["id", "name", "status", "asset_type"]))

        # Get non-existent asset
        r = await client.get(f"/api/v1/assets/{uuid.uuid4()}", headers=HEADERS)
        check("Non-existent asset → 404", r.status_code == 404)

        # List with filters
        r = await client.get("/api/v1/assets/?asset_type=document", headers=HEADERS)
        check("Filter by asset_type works", r.status_code == 200)

        r = await client.get("/api/v1/assets/?limit=5&offset=0", headers=HEADERS)
        check("Pagination params work", r.status_code == 200)

        # Invalid pagination
        r = await client.get("/api/v1/assets/?limit=999", headers=HEADERS)
        check("Overlimit pagination → 422", r.status_code == 422)


        print("\n=== 6. SEARCH ===")
        r = await client.get("/api/v1/assets/search?q=report", headers=HEADERS)
        check("Search works", r.status_code == 200)
        check("Search returns results", "results" in r.json())

        # Empty query
        r = await client.get("/api/v1/assets/search?q=", headers=HEADERS)
        check("Empty search → 422", r.status_code == 422)

        # SQL injection attempt
        r = await client.get("/api/v1/assets/search?q='; DROP TABLE assets; --", headers=HEADERS)
        check("SQL injection safe", r.status_code == 200)

        # XSS attempt
        r = await client.get("/api/v1/assets/search?q=<script>alert(1)</script>", headers=HEADERS)
        check("XSS in search safe", r.status_code == 200)


        print("\n=== 7. GRAPHQL SECURITY ===")
        # No auth
        r = await client.post("/graphql", json={"query": "{ assets { total } }"})
        check("GraphQL no auth → empty", r.status_code == 200 and r.json()["data"]["assets"]["total"] == 0)

        # Valid query
        r = await client.post("/graphql", headers=HEADERS,
                              json={"query": "{ assets(limit: 5) { total assets { id name } } }"})
        check("GraphQL valid query", r.status_code == 200 and "data" in r.json())

        # Introspection (should work in dev)
        r = await client.post("/graphql", json={"query": "{ __schema { types { name } } }"})
        check("GraphQL introspection works", r.status_code == 200)

        # Invalid query
        r = await client.post("/graphql", headers=HEADERS,
                              json={"query": "{ invalidField }"})
        check("GraphQL invalid field → error", "errors" in r.json())

        # GraphQL injection attempt
        r = await client.post("/graphql", headers=HEADERS,
                              json={"query": '{ search(query: "test \\" OR 1=1 --") { total } }'})
        check("GraphQL injection safe", r.status_code == 200)


        print("\n=== 8. LOAD TEST ===")
        # Concurrent requests
        start = time.time()
        tasks = [client.get("/health") for _ in range(50)]
        responses = await asyncio.gather(*tasks)
        elapsed = time.time() - start
        all_ok = all(r.status_code == 200 for r in responses)
        check(f"50 concurrent /health → all 200 ({elapsed:.2f}s)", all_ok)
        check("50 concurrent requests under 3s", elapsed < 3.0)

        # Concurrent asset list
        tasks = [client.get("/api/v1/assets/", headers=HEADERS) for _ in range(20)]
        responses = await asyncio.gather(*tasks)
        all_ok = all(r.status_code == 200 for r in responses)
        check("20 concurrent asset lists → all 200", all_ok)

        # Concurrent uploads init
        start = time.time()
        tasks = [
            client.post("/api/v1/uploads/init", headers=HEADERS, json={
                "filename": f"file_{i}.pdf", "mime_type": "application/pdf", "size_bytes": 1024
            })
            for i in range(10)
        ]
        responses = await asyncio.gather(*tasks)
        elapsed = time.time() - start
        all_ok = all(r.status_code == 201 for r in responses)
        check(f"10 concurrent uploads → all 201 ({elapsed:.2f}s)", all_ok)


        print("\n=== 9. EDGE CASES ===")
        # Very long filename
        r = await client.post("/api/v1/uploads/init", headers=HEADERS, json={
            "filename": "a" * 600, "mime_type": "application/pdf", "size_bytes": 1024
        })
        check("Too long filename → 422", r.status_code == 422)

        # Special chars in filename
        r = await client.post("/api/v1/uploads/init", headers=HEADERS, json={
            "filename": "test file (2024).pdf", "mime_type": "application/pdf", "size_bytes": 1024
        })
        check("Special chars in filename OK", r.status_code == 201)

        # Unknown mime type
        r = await client.post("/api/v1/uploads/init", headers=HEADERS, json={
            "filename": "data.bin", "mime_type": "application/octet-stream", "size_bytes": 1024
        })
        check("Unknown mime type OK", r.status_code == 201)

        # Complete non-existent upload
        r = await client.post(f"/api/v1/uploads/{uuid.uuid4()}/complete",
                              headers=HEADERS,
                              json={"object_key": "fake/key"})
        check("Complete non-existent upload → 404", r.status_code == 404)


        print("\n=== 10. FOLDERS & PROJECTS ===")
        r = await client.post("/api/v1/folders/?name=Documents&parent_id=", headers=HEADERS)
        check("Create folder works", r.status_code == 200)

        r = await client.get("/api/v1/projects/", headers=HEADERS)
        check("List projects works", r.status_code == 200)


    # ── Summary ──────────────────────────────────────────────────
    total = results["passed"] + results["failed"]
    print(f"\n{'='*50}")
    print(f"RESULTS: {results['passed']}/{total} passed")
    if results["errors"]:
        print(f"\nFailed tests:")
        for e in results["errors"]:
            print(f"  ❌ {e}")
    print(f"{'='*50}")


asyncio.run(run_tests())
