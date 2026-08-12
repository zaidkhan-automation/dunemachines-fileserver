"""
GraphQL Schema — Asset graph queries.
Strawberry + FastAPI integration.
"""
import strawberry
from strawberry.fastapi import GraphQLRouter
from strawberry.extensions import AddValidationRules
from graphql import NoSchemaIntrospectionCustomRule
from typing import List, Optional
from datetime import datetime

from app.core.config import settings


# ── Types ─────────────────────────────────────────────────────────

@strawberry.type
class AssetType:
    id: str
    name: str
    asset_type: str
    source_type: str
    status: str
    parent_id: Optional[str] = None
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    summary: Optional[str] = None
    tags: List[str] = strawberry.field(default_factory=list)
    created_at: datetime
    updated_at: datetime


@strawberry.type
class AssetConnection:
    assets: List[AssetType]
    total: int
    has_next: bool
    has_prev: bool


@strawberry.type
class ProjectType:
    id: str
    name: str
    description: Optional[str] = None
    is_public: bool
    created_at: datetime


@strawberry.type
class SearchResult:
    asset: AssetType
    score: float
    highlight: Optional[str] = None


@strawberry.type
class SearchConnection:
    results: List[SearchResult]
    total: int
    query: str
    mode: str


# ── Queries ───────────────────────────────────────────────────────

@strawberry.type
class Query:

    @strawberry.field
    async def asset(self, info, id: str) -> Optional[AssetType]:
        """Get asset by ID."""
        request = info.context["request"]
        user = getattr(request.state, "user", None)
        if not user:
            return None

        from app.core.database import AsyncSessionLocal
        from app.repositories.asset_repo import asset_repo

        async with AsyncSessionLocal() as db:
            asset = await asset_repo.get_by_id(db, id, user["org_id"])
            if not asset:
                return None
            return _asset_to_gql(asset)

    @strawberry.field
    async def assets(
        self, info,
        limit: int = 20,
        offset: int = 0,
        asset_type: Optional[str] = None,
        project_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> AssetConnection:
        """List assets with filtering."""
        request = info.context["request"]
        user = getattr(request.state, "user", None)
        if not user:
            return AssetConnection(assets=[], total=0, has_next=False, has_prev=False)

        from app.core.database import AsyncSessionLocal
        from app.repositories.asset_repo import asset_repo

        async with AsyncSessionLocal() as db:
            items, total = await asset_repo.list(
                db, org_id=user["org_id"],
                asset_type=asset_type,
                project_id=project_id,
                status=status,
                limit=limit,
                offset=offset,
            )
            return AssetConnection(
                assets=[_asset_to_gql(a) for a in items],
                total=total,
                has_next=(offset + limit) < total,
                has_prev=offset > 0,
            )

    @strawberry.field
    async def search(
        self, info,
        query: str,
        mode: str = "hybrid",
        asset_types: Optional[List[str]] = None,
        limit: int = 20,
    ) -> SearchConnection:
        """Search assets — fulltext, semantic, or hybrid."""
        request = info.context["request"]
        user = getattr(request.state, "user", None)
        if not user:
            return SearchConnection(results=[], total=0, query=query, mode=mode)

        from app.core.database import AsyncSessionLocal
        from app.repositories.asset_repo import asset_repo

        from app.services.search.search_service import search_service, SearchMode
        valid_mode = SearchMode(mode) if mode in [m.value for m in SearchMode] else SearchMode.HYBRID
        result = await search_service.search(
            org_id=user["org_id"],
            query=query,
            mode=valid_mode,
            asset_types=asset_types,
            limit=limit,
        )
        # search_service returns dict with asset dicts — convert to gql types
        async with AsyncSessionLocal() as db:
            results = []
            for item in result.get("results", []):
                asset = await asset_repo.get_by_id(db, item.get("id") or item.get("asset_id"), user["org_id"])
                if asset:
                    results.append(SearchResult(
                        asset=_asset_to_gql(asset),
                        score=item.get("score", 1.0)
                    ))
            return SearchConnection(
                results=results,
                total=result.get("total", len(results)),
                query=query,
                mode=valid_mode.value
            )

    @strawberry.field
    async def projects(self, info, limit: int = 50) -> List[ProjectType]:
        """List all projects."""
        request = info.context["request"]
        user = getattr(request.state, "user", None)
        if not user:
            return []

        from app.core.database import AsyncSessionLocal
        from app.repositories.project_repo import project_repo

        async with AsyncSessionLocal() as db:
            items, _ = await project_repo.list(db, org_id=user["org_id"], limit=limit)
            return [
                ProjectType(
                    id=str(p.id),
                    name=p.name,
                    description=p.description,
                    is_public=p.is_public,
                    created_at=p.created_at,
                )
                for p in items
            ]


# ── Mutations ─────────────────────────────────────────────────────

@strawberry.type
class Mutation:

    @strawberry.mutation
    async def create_project(
        self, info, name: str, description: Optional[str] = None, is_public: bool = False
    ) -> ProjectType:
        """Create a new project."""
        request = info.context["request"]
        user = getattr(request.state, "user", None)

        from app.services.permissions.rbac import check_permission
        if not user or not check_permission(user, "projects", "write"):
            raise Exception("Permission denied: projects:write")

        from app.core.database import AsyncSessionLocal
        from app.repositories.project_repo import project_repo
        import uuid as _uuid

        # Duniverse user_ids are integers, not UUIDs — fallback like REST routes do
        created_by = user["user_id"]
        try:
            _uuid.UUID(str(created_by))
        except (ValueError, AttributeError):
            created_by = "00000000-0000-0000-0000-000000000000"

        async with AsyncSessionLocal() as db:
            project = await project_repo.create(db, {
                "organization_id": user["org_id"],
                "created_by": created_by,
                "name": name,
                "description": description,
                "is_public": is_public,
            })
            await db.commit()
            return ProjectType(
                id=str(project.id),
                name=project.name,
                description=project.description,
                is_public=project.is_public,
                created_at=project.created_at,
            )

    @strawberry.mutation
    async def delete_asset(self, info, id: str) -> bool:
        """Soft delete an asset."""
        request = info.context["request"]
        user = getattr(request.state, "user", None)

        from app.services.permissions.rbac import check_permission
        if not user or not check_permission(user, "assets", "delete"):
            raise Exception("Permission denied: assets:delete")

        from app.core.database import AsyncSessionLocal
        from app.repositories.asset_repo import asset_repo

        async with AsyncSessionLocal() as db:
            result = await asset_repo.soft_delete(db, id, user["org_id"])
            await db.commit()
            if result:
                from app.events.producers import publish_event
                from app.events.event_types import EventType
                await publish_event(EventType.ASSET_DELETED, {
                    "asset_id": id,
                    "org_id": user["org_id"],
                })
            return result


# ── Helpers ───────────────────────────────────────────────────────

def _asset_to_gql(asset) -> AssetType:
    return AssetType(
        id=str(asset.id),
        name=asset.name,
        asset_type=asset.asset_type,
        source_type=asset.source_type,
        status=asset.status,
        parent_id=str(asset.parent_id) if asset.parent_id else None,
        mime_type=asset.mime_type,
        size_bytes=asset.size_bytes,
        summary=asset.summary,
        tags=asset.tags or [],
        created_at=asset.created_at,
        updated_at=asset.updated_at,
    )


# ── Schema + Router ───────────────────────────────────────────────

schema = strawberry.Schema(
    query=Query,
    mutation=Mutation,
    extensions=[AddValidationRules([NoSchemaIntrospectionCustomRule])] if not settings.DEBUG else [],
)


async def get_context(request):
    """Inject user into GraphQL context from JWT."""

    user = None
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        try:
            from app.core.security import decode_token, resolve_identity
            payload = decode_token(token)
            identity = await resolve_identity(payload)
            user = {
                "user_id": identity["user_id"],
                "org_id": identity["org_id"],
                "email": payload.get("email", ""),
                "roles": payload.get("roles", []),
            }
        except Exception:
            try:
                from app.core.security import decode_duniverse_token, resolve_identity
                payload = decode_duniverse_token(token)
                identity = await resolve_identity(payload)
                user = {
                    "user_id": identity["user_id"],
                    "org_id": identity["org_id"],
                    "email": payload.get("email", ""),
                    "roles": payload.get("roles", []),
                }
            except Exception:
                pass

    request.state.user = user
    return {"request": request}


from fastapi import Request

async def get_graphql_context(request: Request):
    return await get_context(request)

graphql_router = GraphQLRouter(
    schema,
    context_getter=get_graphql_context,
    graphiql=settings.DEBUG,
)
