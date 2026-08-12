"""
One-time recovery tool for assets stranded in the legacy shared org bucket
(00000000-0000-0000-0000-000000000001 or a0000000-0000-0000-0000-000000000001)
after the per-user org isolation fix in app/core/security.py.

This is deliberately NOT an automated migration. There is no reliable data
in the DB (or logs) that maps most of those legacy assets back to the
Duniverse user who actually created them — 656 of 709 assets in the main
legacy bucket already share one collapsed placeholder created_by, and the
remaining ones don't match any known user. Guessing would risk silently
handing one person's file to someone else's now-isolated bucket.

Instead: a human (the asset's actual owner, or someone who can confirm on
their behalf) identifies specific asset_ids that are theirs, by name/path
they recognize. Those confirmed (asset_id -> duniverse_username) pairs go
in CLAIMS below (or are passed via --claims-file as JSON). The script
reassigns organization_id/created_by for exactly those rows, using the
same resolve_identity() derivation the live app uses, so the file lands
in the org the user will actually see.

Usage:
    python3 scripts/reassign_bridged_assets.py --dry-run   # always run this first
    python3 scripts/reassign_bridged_assets.py --apply
"""
import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, update
from app.core.database import AsyncSessionLocal
from app.core.security import resolve_identity
from app.models.asset import Asset

# Verified (asset_id -> duniverse username) claims. Populate this after the
# owner has confirmed which specific assets are theirs — do not guess.
# Example:
# CLAIMS = {
#     "3f9a1b2c-...": "ana01",
#     "7d2e4f8a-...": "admin",
# }
CLAIMS: dict[str, str] = {}


async def main(claims: dict[str, str], apply: bool):
    if not claims:
        print("No claims provided (CLAIMS is empty and no --claims-file given). Nothing to do.")
        return

    async with AsyncSessionLocal() as db:
        for asset_id, username in claims.items():
            try:
                asset_uuid = uuid.UUID(asset_id)
            except ValueError:
                print(f"SKIP  {asset_id}: not a valid UUID")
                continue

            result = await db.execute(select(Asset).where(Asset.id == asset_uuid))
            asset = result.scalar_one_or_none()
            if not asset:
                print(f"SKIP  {asset_id}: no such asset")
                continue

            identity = await resolve_identity({"sub": username})
            new_org = uuid.UUID(identity["org_id"])
            new_creator = uuid.UUID(identity["user_id"])

            print(
                f"{'APPLY' if apply else 'DRY-RUN'}  {asset.name!r} ({asset_id}) "
                f"org {asset.organization_id} -> {new_org}, "
                f"created_by {asset.created_by} -> {new_creator}  [claimed by {username}]"
            )

            if apply:
                await db.execute(
                    update(Asset)
                    .where(Asset.id == asset_uuid)
                    .values(organization_id=new_org, created_by=new_creator)
                )

        if apply:
            await db.commit()
            print(f"\nReassigned {len(claims)} asset(s).")
        else:
            print(f"\nDry run — {len(claims)} asset(s) would be reassigned. Re-run with --apply to commit.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Actually write changes (default is dry-run)")
    parser.add_argument("--claims-file", type=str, help="JSON file of {asset_id: duniverse_username}")
    args = parser.parse_args()

    claims = dict(CLAIMS)
    if args.claims_file:
        claims.update(json.loads(Path(args.claims_file).read_text()))

    asyncio.run(main(claims, apply=args.apply))
