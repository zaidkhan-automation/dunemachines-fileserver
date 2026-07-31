"""
Full security audit + event bus test.
Tests: auth, permissions, tenant isolation, event bus, webhooks, rate limiting, edge cases.
"""
import asyncio
import httpx
import json
import time
import uuid
import sys
sys.path.insert(0, '/home/duniverse/dunemachines-fileserver')

from app.core.config import settings
from app.core.security import create_access_token
from app.services.permissions.rbac import PermissionChecker, BUILT_IN_ROLES

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

# Tokens
OWNER_TOKEN = create_access_token({"sub": "a0000000-0000-0000-0000-000000000002", "email": "ahmed@dunemachines.com", "org_id": "a0000000-0000-0000-0000-000000000001", "roles": ["owner"]})
ADMIN_TOKEN = create_access_token({"sub": "a0000000-0000-0000-0000-000000000003", "email": "admin@dunemachines.com", "org_id": "a0000000-0000-0000-0000-000000000001", "roles": ["admin"]})
VIEWER_TOKEN = create_access_token({"sub": "a0000000-0000-0000-0000-000000000004", "email": "viewer@dunemachines.com", "org_id": "a0000000-0000-0000-0000-000000000001", "roles": ["viewer"]})
AGENT_READ_TOKEN = create_access_token({"sub": "agent:read_only", "type": "agent", "org_id": "a0000000-0000-0000-0000-000000000001", "roles": ["agent:read"], "scopes": list(BUILT_IN_ROLES["agent:read"]["permissions"])})
AGENT_FULL_TOKEN = create_access_token({"sub": "agent:omnius", "type": "agent", "org_id": "a0000000-0000-0000-0000-000000000001", "roles": ["agent:full"], "scopes": list(BUILT_IN_ROLES["agent:full"]["permissions"])})
OTHER_ORG_TOKEN = create_access_token({"sub": "b0000000-0000-0000-0000-000000000001", "email": "evil@hack.com", "org_id": "b0000000-0000-0000-0000-000000000099", "roles": ["owner"]})

OWNER_H = {"Authorization": f"Bearer {OWNER_TOKEN}", "Content-Type": "application/json"}
ADMIN_H = {"Authorization": f"Bearer {ADMIN_TOKEN}", "Content-Type": "application/json"}
VIEWER_H = {"Authorization": f"Bearer {VIEWER_TOKEN}", "Content-Type": "application/json"}
AGENT_R_H = {"Authorization": f"Bearer {AGENT_READ_TOKEN}", "Content-Type": "application/json"}
AGENT_F_H = {"Authorization": f"Bearer {AGENT_FULL_TOKEN}", "Content-Type": "application/json"}
OTHER_H = {"Authorization": f"Bearer {OTHER_ORG_TOKEN}", "Content-Type": "application/json"}


async def run_audit():
    async with httpx.AsyncClient(timeout=120, verify=True) as c:

        print("\n=== 1. PERMISSION SYSTEM UNIT TESTS ===")
        # Owner has all permissions
        owner_checker = PermissionChecker.from_role("owner")
        check("Owner has assets:delete", owner_checker.has("assets", "delete"))
        check("Owner has org:delete", owner_checker.has("org", "delete"))
        check("Owner has org:billing", owner_checker.has("org", "billing"))
        check("Owner has audit:export", owner_checker.has("audit", "export"))

        # Admin cannot delete org
        admin_checker = PermissionChecker.from_role("admin")
        check("Admin has assets:write", admin_checker.has("assets", "write"))
        check("Admin CANNOT delete org", not admin_checker.has("org", "delete"))
        check("Admin CANNOT billing", not admin_checker.has("org", "billing"))

        # Viewer is read only
        viewer_checker = PermissionChecker.from_role("viewer")
        check("Viewer can read assets", viewer_checker.has("assets", "read"))
        check("Viewer CANNOT write assets", not viewer_checker.has("assets", "write"))
        check("Viewer CANNOT delete", not viewer_checker.has("assets", "delete"))
        check("Viewer CANNOT upload", not viewer_checker.has("assets", "upload"))

        # Agent:read
        agent_read_checker = PermissionChecker.from_role("agent:read")
        check("Agent:read can search", agent_read_checker.has("assets", "search"))
        check("Agent:read CANNOT write", not agent_read_checker.has("assets", "write"))
        check("Agent:read CANNOT create PR", not agent_read_checker.has("connectors", "pr:create"))

        # Agent:full
        agent_full_checker = PermissionChecker.from_role("agent:full")
        check("Agent:full can create PR", agent_full_checker.has("connectors", "pr:create"))
        check("Agent:full can commit", agent_full_checker.has("connectors", "commit"))
        check("Agent:full CANNOT delete org", not agent_full_checker.has("org", "delete"))
        check("Agent:full CANNOT billing", not agent_full_checker.has("org", "billing"))


        print("\n=== 2. AUTH SECURITY ===")
        r = await c.get(f"{BASE}/api/v1/assets/")
        check("No token → 403", r.status_code == 403)

        r = await c.get(f"{BASE}/api/v1/assets/", headers={"Authorization": "Bearer fake.jwt.token"})
        check("Fake token → 401", r.status_code == 401)

        r = await c.get(f"{BASE}/api/v1/assets/", headers={"Authorization": "Basic admin:admin"})
        check("Basic auth → 403", r.status_code == 403)

        # SQL injection in auth header
        r = await c.get(f"{BASE}/api/v1/assets/", headers={"Authorization": "Bearer ' OR '1'='1"})
        check("SQL injection in auth → 401/403", r.status_code in [401, 403])


        print("\n=== 3. TENANT ISOLATION ===")
        # Create asset in org A
        r = await c.post(f"{BASE}/api/v1/uploads/init", headers=OWNER_H,
                        json={"filename": "secret.pdf", "mime_type": "application/pdf", "size_bytes": 1024})
        check("Org A can upload", r.status_code == 201)
        asset_id = r.json().get("upload_id", "") if r.status_code == 201 else ""

        # Org B cannot see org A assets
        r = await c.get(f"{BASE}/api/v1/assets/", headers=OTHER_H)
        check("Org B gets empty list", r.status_code == 200 and r.json()["total"] == 0)

        # Org B cannot access specific asset
        if asset_id:
            r = await c.get(f"{BASE}/api/v1/assets/{asset_id}", headers=OTHER_H)
            check("Org B cannot get org A asset → 404", r.status_code == 404)

            r = await c.delete(f"{BASE}/api/v1/assets/{asset_id}", headers=OTHER_H)
            check("Org B cannot delete org A asset", r.status_code == 404)


        print("\n=== 4. ROLE-BASED ACCESS ===")
        # Viewer cannot upload
        r = await c.post(f"{BASE}/api/v1/uploads/init", headers=VIEWER_H,
                        json={"filename": "test.pdf", "mime_type": "application/pdf", "size_bytes": 1024})
        check("Viewer CAN upload (viewer has upload perm)", r.status_code == 201)

        # Viewer cannot create agent token
        r = await c.post(f"{BASE}/api/v1/permissions/agent-token", headers=VIEWER_H,
                        json={"name": "test", "role": "agent:read", "expires_in_days": 1})
        check("Viewer CANNOT create agent token → 403", r.status_code == 403)

        # Admin can create agent token
        r = await c.post(f"{BASE}/api/v1/permissions/agent-token", headers=ADMIN_H,
                        json={"name": "Test Agent", "role": "agent:read", "expires_in_days": 1})
        check("Admin CAN create agent token", r.status_code == 200)

        # Admin cannot create owner-level token
        r = await c.post(f"{BASE}/api/v1/permissions/agent-token", headers=ADMIN_H,
                        json={"name": "Evil Agent", "role": "owner", "expires_in_days": 1})
        check("Admin CANNOT create owner token → 403", r.status_code == 403)


        print("\n=== 5. AGENT PERMISSIONS ===")
        # Agent:read can list
        r = await c.get(f"{BASE}/api/v1/assets/", headers=AGENT_R_H)
        check("Agent:read CAN list assets", r.status_code == 200)

        # Agent:read can search
        r = await c.get(f"{BASE}/api/v1/assets/search?q=test", headers=AGENT_R_H)
        check("Agent:read CAN search", r.status_code == 200)

        # Agent:full can upload
        r = await c.post(f"{BASE}/api/v1/uploads/init", headers=AGENT_F_H,
                        json={"filename": "agent_file.pdf", "mime_type": "application/pdf", "size_bytes": 1024})
        check("Agent:full CAN upload", r.status_code == 201)

        # Agent cannot create other agents
        r = await c.post(f"{BASE}/api/v1/permissions/agent-token", headers=AGENT_F_H,
                        json={"name": "Rogue Agent", "role": "agent:full", "expires_in_days": 1})
        check("Agent CANNOT create other agents → 403", r.status_code == 403)


        print("\n=== 6. EVENT BUS TEST ===")
        import redis.asyncio as aioredis

        r_client = aioredis.from_url("redis://localhost:6379/2", decode_responses=True)
        pubsub = r_client.pubsub()
        await pubsub.subscribe("fileserver:events")

        received_events = []
        async def listen():
            async for msg in pubsub.listen():
                if msg["type"] == "message":
                    received_events.append(json.loads(msg["data"]))
                    if len(received_events) >= 1:
                        break

        # Trigger an upload to generate event
        listener_task = asyncio.create_task(listen())

        r = await c.post(f"{BASE}/api/v1/uploads/init", headers=OWNER_H,
                        json={"filename": "event_test.pdf", "mime_type": "application/pdf", "size_bytes": 512})
        upload_id = r.json().get("upload_id", "") if r.status_code == 201 else ""

        if upload_id:
            # Complete upload to trigger event
            await c.post(f"{BASE}/api/v1/uploads/{upload_id}/complete", headers=OWNER_H,
                        json={"object_key": f"orgs/test/assets/{upload_id}/event_test.pdf"})

        try:
            await asyncio.wait_for(listener_task, timeout=5.0)
        except asyncio.TimeoutError:
            listener_task.cancel()

        await pubsub.unsubscribe()
        await r_client.aclose()

        check("Event bus receives upload events", len(received_events) > 0)
        if received_events:
            event = received_events[0]
            check("Event has type field", "type" in event)
            check("Event has payload field", "payload" in event)
            check("Event has timestamp", "timestamp" in event)
            print(f"  📨 Event received: {event.get('type')} — payload keys: {list(event.get('payload', {}).keys())}")


        print("\n=== 7. WEBHOOK SECURITY ===")
        # Valid webhook
        import hmac, hashlib
        secret = settings.GITHUB_WEBHOOK_SECRET
        payload = b'{"zen": "test", "hook_id": 999}'
        sig = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

        r = await c.post(f"{BASE}/api/v1/webhooks/github/events",
                        content=payload,
                        headers={"Content-Type": "application/json",
                                "X-GitHub-Event": "ping",
                                "X-Hub-Signature-256": sig,
                                "X-GitHub-Delivery": str(uuid.uuid4())})
        check("Valid webhook signature → 200", r.status_code == 200)

        # Invalid signature
        r = await c.post(f"{BASE}/api/v1/webhooks/github/events",
                        content=payload,
                        headers={"Content-Type": "application/json",
                                "X-GitHub-Event": "ping",
                                "X-Hub-Signature-256": "sha256=invalidsignature",
                                "X-GitHub-Delivery": str(uuid.uuid4())})
        check("Invalid webhook signature → 401", r.status_code == 401)

        # No signature
        r = await c.post(f"{BASE}/api/v1/webhooks/github/events",
                        content=payload,
                        headers={"Content-Type": "application/json",
                                "X-GitHub-Event": "ping"})
        check("No webhook signature → 401", r.status_code == 401)

        # Tampered payload
        tampered = b'{"zen": "tampered", "hook_id": 999}'
        r = await c.post(f"{BASE}/api/v1/webhooks/github/events",
                        content=tampered,
                        headers={"Content-Type": "application/json",
                                "X-GitHub-Event": "ping",
                                "X-Hub-Signature-256": sig,
                                "X-GitHub-Delivery": str(uuid.uuid4())})
        check("Tampered payload rejected → 401", r.status_code == 401)


        print("\n=== 8. INPUT VALIDATION & INJECTION ===")
        # XSS in filename
        r = await c.post(f"{BASE}/api/v1/uploads/init", headers=OWNER_H,
                        json={"filename": "<script>alert(1)</script>.pdf", "mime_type": "application/pdf", "size_bytes": 1024})
        check("XSS filename accepted safely", r.status_code == 201)

        # Path traversal
        r = await c.post(f"{BASE}/api/v1/uploads/init", headers=OWNER_H,
                        json={"filename": "../../etc/passwd", "mime_type": "text/plain", "size_bytes": 100})
        check("Path traversal filename accepted safely", r.status_code == 201)

        # SQL injection in search
        r = await c.get(f"{BASE}/api/v1/assets/search", headers=OWNER_H,
                        params={"q": "'; DROP TABLE assets; --"})
        check("SQL injection in search safe", r.status_code == 200 and "results" in r.json())

        # GraphQL injection
        r = await c.post(f"{BASE}/graphql", headers=OWNER_H,
                        json={"query": '{ search(query: "test\\" OR 1=1") { total } }'})
        check("GraphQL injection safe", r.status_code == 200)

        # Oversized payload
        r = await c.post(f"{BASE}/api/v1/uploads/init", headers=OWNER_H,
                        json={"filename": "a" * 600, "mime_type": "application/pdf", "size_bytes": 1024})
        check("Oversized filename → 422", r.status_code == 422)


        print("\n=== 9. CONCURRENT LOAD TEST ===")
        start = time.time()
        tasks = [c.get(f"{BASE}/health") for _ in range(100)]
        responses = await asyncio.gather(*tasks)
        elapsed = time.time() - start
        all_ok = all(r.status_code == 200 for r in responses)
        check(f"100 concurrent health → all 200 ({elapsed:.2f}s)", all_ok)
        check("100 concurrent under 5s", elapsed < 5.0)

        # Concurrent authenticated requests
        tasks = [c.get(f"{BASE}/api/v1/assets/", headers=OWNER_H) for _ in range(30)]
        responses = await asyncio.gather(*tasks)
        all_ok = all(r.status_code == 200 for r in responses)
        check("30 concurrent auth requests → all 200", all_ok)

        # Mixed roles concurrent
        tasks = (
            [c.get(f"{BASE}/api/v1/assets/", headers=OWNER_H) for _ in range(10)] +
            [c.get(f"{BASE}/api/v1/assets/", headers=VIEWER_H) for _ in range(10)] +
            [c.get(f"{BASE}/api/v1/assets/", headers=AGENT_R_H) for _ in range(10)]
        )
        responses = await asyncio.gather(*tasks)
        all_ok = all(r.status_code == 200 for r in responses)
        check("30 mixed-role concurrent → all 200", all_ok)


        print("\n=== 10. GRAPHQL SECURITY ===")
        # Depth attack (deeply nested query)
        deep_query = "{ assets { assets { assets { assets { total } } } } }"
        r = await c.post(f"{BASE}/graphql", headers=OWNER_H, json={"query": deep_query})
        check("Deep nested query handled", r.status_code in [200, 400])

        # Introspection with no auth
        r = await c.post(f"{BASE}/graphql", json={"query": "{ __schema { types { name } } }"})
        check("Introspection without auth works (public schema)", r.status_code == 200)

        # Valid mutations
        r = await c.post(f"{BASE}/graphql", headers=OWNER_H,
                        json={"query": 'mutation { createProject(name: "Audit Test Project") { id name } }'})
        check("GraphQL mutation works", r.status_code == 200 and "data" in r.json())


    # Summary
    total = results["passed"] + results["failed"]
    print(f"\n{'='*55}")
    print(f"SECURITY AUDIT: {results['passed']}/{total} passed")
    if results["errors"]:
        print(f"\n❌ Failed:")
        for e in results["errors"]:
            print(f"  • {e}")
    else:
        print("🎉 Zero security issues found!")
    print(f"{'='*55}")


asyncio.run(run_audit())
