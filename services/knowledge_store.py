"""Owned topic/entity/file/OCR repositories for the PostgreSQL control plane."""

from __future__ import annotations

import json
from typing import Any

from services.architecture_store import ArchitectureStore


class TopicStore:
    def __init__(self, store: ArchitectureStore | None = None):
        self.store = store or ArchitectureStore()

    def upsert(
        self,
        *,
        user_id: str,
        conversation_id: str,
        title: str,
        canonical_topic: str,
        summary: str,
        source_message_ids: list[str],
        linked_entities: list[str] | None = None,
        linked_memories: list[str] | None = None,
    ) -> str:
        with self.store.transaction() as connection:
            self.store.ensure_user(connection, user_id)
            row = connection.execute(
                """
                INSERT INTO conversation_topics (
                    user_id, conversation_id, title, canonical_topic, summary,
                    linked_entities, linked_memories, source_message_ids
                ) VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb)
                ON CONFLICT (user_id, conversation_id, canonical_topic) DO UPDATE SET
                    title=excluded.title,
                    summary=excluded.summary,
                    linked_entities=conversation_topics.linked_entities || excluded.linked_entities,
                    linked_memories=conversation_topics.linked_memories || excluded.linked_memories,
                    source_message_ids=conversation_topics.source_message_ids || excluded.source_message_ids,
                    last_seen_at=now()
                RETURNING topic_id
                """,
                (
                    user_id, conversation_id, title[:240], canonical_topic[:240],
                    summary[:4000], json.dumps(linked_entities or []),
                    json.dumps(linked_memories or []), json.dumps(source_message_ids),
                ),
            ).fetchone()
        return str(row["topic_id"])


class EntityGraphStore:
    VALID_TYPES = {
        "PERSON", "DEVICE", "PROJECT", "PLACE", "ORGANIZATION", "GAME",
        "MOVIE", "COURSE", "SKILL", "GOAL", "EVENT", "ASSET",
    }
    CLAIM_STATUSES = {"SELF_REPORTED", "PROVISIONAL", "VERIFIED", "DISPUTED", "REJECTED"}

    def __init__(self, store: ArchitectureStore | None = None):
        self.store = store or ArchitectureStore()

    def create_entity(
        self,
        *,
        user_id: str,
        entity_type: str,
        canonical_name: str,
        attributes: dict[str, Any] | None = None,
        claim_status: str = "SELF_REPORTED",
        sensitivity: str = "NORMAL_USER_DATA",
        visibility_scope: str = "PRIVATE_TO_SOURCE_USER",
    ) -> str:
        if entity_type not in self.VALID_TYPES or claim_status not in self.CLAIM_STATUSES:
            raise ValueError("Invalid entity classification.")
        with self.store.transaction() as connection:
            self.store.ensure_user(connection, user_id)
            existing = connection.execute(
                """
                SELECT entity_id FROM entities
                WHERE user_id=%s AND entity_type=%s
                  AND lower(canonical_name)=lower(%s) AND deleted_at IS NULL
                FOR UPDATE
                """,
                (user_id, entity_type, canonical_name),
            ).fetchone()
            if existing:
                connection.execute(
                    """
                    UPDATE entities SET attributes=attributes || %s::jsonb,
                        updated_at=now(), version=version+1
                    WHERE entity_id=%s
                    """,
                    (json.dumps(attributes or {}), existing["entity_id"]),
                )
                return str(existing["entity_id"])
            row = connection.execute(
                """
                INSERT INTO entities (
                    user_id, entity_type, canonical_name, attributes,
                    claim_status, sensitivity, visibility_scope
                ) VALUES (%s,%s,%s,%s::jsonb,%s,%s,%s)
                RETURNING entity_id
                """,
                (
                    user_id, entity_type, canonical_name[:240],
                    json.dumps(attributes or {}), claim_status, sensitivity, visibility_scope,
                ),
            ).fetchone()
        return str(row["entity_id"])

    def add_alias(
        self, *, user_id: str, entity_id: str, alias: str, pronunciation: str | None = None
    ) -> None:
        with self.store.transaction() as connection:
            owned = connection.execute(
                "SELECT 1 FROM entities WHERE entity_id=%s AND user_id=%s AND deleted_at IS NULL",
                (entity_id, user_id),
            ).fetchone()
            if not owned:
                raise PermissionError("Entity is not owned by the authenticated user.")
            connection.execute(
                """
                INSERT INTO entity_aliases (entity_id, user_id, alias, pronunciation)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT (user_id, entity_id, alias) DO UPDATE
                    SET pronunciation=COALESCE(excluded.pronunciation, entity_aliases.pronunciation)
                """,
                (entity_id, user_id, alias[:240], pronunciation[:240] if pronunciation else None),
            )

    def link(
        self,
        *,
        user_id: str,
        source_entity_id: str,
        target_entity_id: str,
        relationship_type: str,
        claim_status: str = "SELF_REPORTED",
        visibility_scope: str = "PRIVATE_TO_SOURCE_USER",
    ) -> str:
        if claim_status not in self.CLAIM_STATUSES:
            raise ValueError("Invalid relationship claim status.")
        with self.store.transaction() as connection:
            owned_count = connection.execute(
                """
                SELECT count(*) AS count FROM entities
                WHERE user_id=%s AND entity_id=ANY(%s) AND deleted_at IS NULL
                """,
                (user_id, [source_entity_id, target_entity_id]),
            ).fetchone()["count"]
            if owned_count != 2:
                raise PermissionError("Cross-user entity linking is not permitted.")
            row = connection.execute(
                """
                INSERT INTO entity_relationships (
                    user_id, source_entity_id, target_entity_id,
                    relationship_type, claim_status, visibility_scope
                ) VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (user_id, source_entity_id, target_entity_id, relationship_type)
                DO UPDATE SET claim_status=excluded.claim_status,
                    visibility_scope=excluded.visibility_scope, updated_at=now()
                RETURNING relationship_id
                """,
                (
                    user_id, source_entity_id, target_entity_id,
                    relationship_type[:120], claim_status, visibility_scope,
                ),
            ).fetchone()
        return str(row["relationship_id"])


class FileMetadataStore:
    def __init__(self, store: ArchitectureStore | None = None):
        self.store = store or ArchitectureStore()

    def register(
        self,
        *,
        user_id: str,
        conversation_id: str | None,
        original_name: str,
        mime_type: str,
        size: int,
        storage_location: str,
        sha256: str,
    ) -> str:
        with self.store.transaction() as connection:
            self.store.ensure_user(connection, user_id)
            row = connection.execute(
                """
                INSERT INTO stored_files (
                    user_id, conversation_id, original_name, mime_type,
                    size, storage_location, sha256
                ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (user_id, sha256, status) DO UPDATE
                    SET storage_location=stored_files.storage_location
                RETURNING file_id
                """,
                (
                    user_id, conversation_id, original_name[:300], mime_type[:200],
                    size, storage_location, sha256,
                ),
            ).fetchone()
        return str(row["file_id"])

    def cached_ocr(
        self, *, user_id: str, page_hash: str, provider: str, version: str
    ) -> dict[str, Any] | None:
        with self.store._connect() as connection:
            row = connection.execute(
                """
                SELECT extracted_text, layout_data FROM ocr_cache
                WHERE user_id=%s AND page_hash=%s
                  AND ocr_provider=%s AND ocr_version=%s
                """,
                (user_id, page_hash, provider, version),
            ).fetchone()
        return dict(row) if row else None

    def save_ocr(
        self,
        *,
        user_id: str,
        source_file_id: str,
        file_hash: str,
        page_number: int,
        page_hash: str,
        provider: str,
        version: str,
        extracted_text: str,
        layout_data: dict[str, Any],
    ) -> None:
        with self.store.transaction() as connection:
            owned = connection.execute(
                "SELECT 1 FROM stored_files WHERE file_id=%s AND user_id=%s AND deleted_at IS NULL",
                (source_file_id, user_id),
            ).fetchone()
            if not owned:
                raise PermissionError("Source file is not owned by the authenticated user.")
            connection.execute(
                """
                INSERT INTO ocr_cache (
                    source_file_id, user_id, file_hash, page_number, page_hash,
                    ocr_provider, ocr_version, extracted_text, layout_data
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT (user_id, page_hash, ocr_provider, ocr_version) DO NOTHING
                """,
                (
                    source_file_id, user_id, file_hash, page_number, page_hash,
                    provider, version, extracted_text, json.dumps(layout_data),
                ),
            )
