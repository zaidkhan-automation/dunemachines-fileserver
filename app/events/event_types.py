"""
All system event types — used by producers and consumers.
"""
from enum import Enum


class EventType(str, Enum):
    # Asset lifecycle
    ASSET_CREATED = "asset.created"
    ASSET_UPDATED = "asset.updated"
    ASSET_DELETED = "asset.deleted"
    ASSET_UPLOAD_COMPLETE = "asset.upload_complete"
    ASSET_PROCESSING_STARTED = "asset.processing_started"
    ASSET_PROCESSING_COMPLETE = "asset.processing_complete"
    ASSET_PROCESSING_FAILED = "asset.processing_failed"

    # Version events
    VERSION_CREATED = "version.created"

    # Search events
    ASSET_INDEX_REQUESTED = "asset.index_requested"
    ASSET_INDEX_COMPLETE = "asset.index_complete"

    # Live content events — Phase 2 (agent live edits/writes)
    FILE_CONTENT_WRITE = "file.write"   # full content replace
    FILE_CONTENT_EDIT = "file.edit"     # diff/patch applied

    # Connector events
    CONNECTOR_SYNC_STARTED = "connector.sync_started"
    CONNECTOR_SYNC_COMPLETE = "connector.sync_complete"
    CONNECTOR_SYNC_FAILED = "connector.sync_failed"

    # AI events
    AI_EMBEDDING_REQUESTED = "ai.embedding_requested"
    AI_SUMMARY_REQUESTED = "ai.summary_requested"
    AI_CLASSIFICATION_REQUESTED = "ai.classification_requested"
