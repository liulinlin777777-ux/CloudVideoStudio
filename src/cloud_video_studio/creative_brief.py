"""Versioned, local-only P3-01 creative brief and research provenance artifact.

Source records are entered by the user. This module never fetches URLs or
claims that citations have been independently verified.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
MAX_ARTIFACT_BYTES = 256 * 1024
MAX_SOURCES = 50
MAX_CLAIMS = 100
PROJECT_NAMESPACE = uuid.UUID("c32cc37f-f289-46c1-90e7-9a7f348875d4")
ID_RE = re.compile(r"^[0-9a-f]{32}$")
SOURCE_TYPES = {"webpage", "paper", "book", "video", "interview", "dataset", "other"}
BRIEF_FIELDS = {
    "schema_version", "project_id", "brief_id", "audience", "goal", "topic",
    "key_message", "tone", "target_duration_seconds", "constraints", "sources", "claims",
}
SOURCE_FIELDS = {"id", "source_type", "title", "publisher", "citation", "locator", "notes"}
CLAIM_FIELDS = {"id", "text", "source_ids"}


def brief_id_for_project(project_id: str) -> str:
    if not isinstance(project_id, str) or not project_id.strip() or len(project_id) > 160:
        raise ValueError("invalid project id")
    return uuid.uuid5(PROJECT_NAMESPACE, project_id).hex


def _text(value: Any, name: str, *, required: bool = False, limit: int = 4000) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{name} is required")
    if len(value) > limit:
        raise ValueError(f"{name} exceeds {limit} characters")
    return value


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ValueError(f"{name} must be a 32-character lowercase hexadecimal id")
    return value


def _normalize_input(data: Any, expected_project_id: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("brief must be an object")
    allowed = BRIEF_FIELDS | {"created_at", "updated_at", "review"}
    if set(data) - allowed:
        raise ValueError("brief contains unsupported fields")
    if type(data.get("schema_version")) is not int or data["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported creative brief schema version")
    if data.get("project_id") != expected_project_id:
        raise ValueError("brief project id does not match the current project")
    if data.get("brief_id") != brief_id_for_project(expected_project_id):
        raise ValueError("brief id does not match the current project")

    sources = data.get("sources", [])
    claims = data.get("claims", [])
    if not isinstance(sources, list) or len(sources) > MAX_SOURCES:
        raise ValueError(f"sources must be a list of at most {MAX_SOURCES} records")
    if not isinstance(claims, list) or len(claims) > MAX_CLAIMS:
        raise ValueError(f"claims must be a list of at most {MAX_CLAIMS} records")

    normalized_sources = []
    source_ids: set[str] = set()
    for source in sources:
        if not isinstance(source, dict) or set(source) != SOURCE_FIELDS:
            raise ValueError("each source must contain exactly the supported citation fields")
        source_id = _identifier(source.get("id"), "source id")
        if source_id in source_ids:
            raise ValueError("source ids must be unique")
        source_ids.add(source_id)
        source_type = source.get("source_type")
        if not isinstance(source_type, str) or source_type not in SOURCE_TYPES:
            raise ValueError("unsupported source type")
        normalized_sources.append({
            "id": source_id,
            "source_type": source_type,
            "title": _text(source.get("title"), "source title", required=True, limit=300),
            "publisher": _text(source.get("publisher"), "source publisher", limit=300),
            "citation": _text(source.get("citation"), "source citation", required=True, limit=2000),
            "locator": _text(source.get("locator"), "source locator", limit=500),
            "notes": _text(source.get("notes"), "source notes", limit=2000),
        })

    normalized_claims = []
    claim_ids: set[str] = set()
    for claim in claims:
        if not isinstance(claim, dict) or set(claim) != CLAIM_FIELDS:
            raise ValueError("each claim must contain exactly text and citation references")
        claim_id = _identifier(claim.get("id"), "claim id")
        if claim_id in claim_ids:
            raise ValueError("claim ids must be unique")
        claim_ids.add(claim_id)
        refs = claim.get("source_ids")
        if not isinstance(refs, list) or not refs:
            raise ValueError("every factual claim needs at least one source citation")
        normalized_refs = [_identifier(ref, "claim source id") for ref in refs]
        if len(set(normalized_refs)) != len(normalized_refs):
            raise ValueError("claim citations must be unique")
        if any(ref not in source_ids for ref in normalized_refs):
            raise ValueError("claim references a missing source record")
        normalized_claims.append({
            "id": claim_id,
            "text": _text(claim.get("text"), "claim text", required=True, limit=2000),
            "source_ids": normalized_refs,
        })

    duration = data.get("target_duration_seconds", 60)
    if type(duration) is not int or not 1 <= duration <= 3600:
        raise ValueError("target duration must be an integer from 1 to 3600 seconds")
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": expected_project_id,
        "brief_id": brief_id_for_project(expected_project_id),
        "audience": _text(data.get("audience", ""), "audience", limit=1000),
        "goal": _text(data.get("goal", ""), "goal", limit=1000),
        "topic": _text(data.get("topic", ""), "topic", limit=1000),
        "key_message": _text(data.get("key_message", ""), "key message", limit=2000),
        "tone": _text(data.get("tone", ""), "tone", limit=300),
        "target_duration_seconds": duration,
        "constraints": _text(data.get("constraints", ""), "constraints", limit=2000),
        "sources": normalized_sources,
        "claims": normalized_claims,
    }


def _valid_timestamp(value: Any, name: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return value


def default_brief(project_id: str) -> dict[str, Any]:
    return {
        **_normalize_input({
            "schema_version": SCHEMA_VERSION,
            "project_id": project_id,
            "brief_id": brief_id_for_project(project_id),
        }, project_id),
        "created_at": None,
        "updated_at": None,
        "review": {"status": "draft", "approved_at": None},
    }


def validate_stored_brief(data: Any, expected_project_id: str) -> dict[str, Any]:
    normalized = _normalize_input(data, expected_project_id)
    if not isinstance(data, dict) or set(data) != BRIEF_FIELDS | {"created_at", "updated_at", "review"}:
        raise ValueError("stored brief has missing or unsupported fields")
    created_at = _valid_timestamp(data["created_at"], "created_at", nullable=True)
    updated_at = _valid_timestamp(data["updated_at"], "updated_at", nullable=True)
    review = data["review"]
    if not isinstance(review, dict) or set(review) != {"status", "approved_at"}:
        raise ValueError("stored review record is invalid")
    if review["status"] not in {"draft", "approved"}:
        raise ValueError("stored review status is invalid")
    approved_at = _valid_timestamp(review["approved_at"], "approved_at", nullable=True)
    if (review["status"] == "approved") != (approved_at is not None):
        raise ValueError("stored review status and approval time disagree")
    return {
        **normalized,
        "created_at": created_at,
        "updated_at": updated_at,
        "review": {"status": review["status"], "approved_at": approved_at},
    }


def artifact_path(root: Path, project_id: str) -> Path:
    key = hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:32]
    return root / f"creative-brief-{key}.json"


def artifact_sha256(data: dict[str, Any]) -> str:
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def read_brief(path: Path, project_id: str) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError("creative brief artifact must not be a symlink")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return default_brief(project_id)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ARTIFACT_BYTES:
            raise ValueError("creative brief artifact is not a bounded regular file")
        chunks = []
        remaining = MAX_ARTIFACT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_ARTIFACT_BYTES:
            raise ValueError("creative brief artifact exceeds the size limit")
        return validate_stored_brief(json.loads(raw.decode("utf-8")), project_id)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("creative brief artifact is not valid UTF-8 JSON") from error
    finally:
        os.close(descriptor)


def save_brief(path: Path, data: Any, project_id: str, *, approve: bool = False) -> dict[str, Any]:
    existing = read_brief(path, project_id)
    normalized = _normalize_input(data, project_id)
    if approve:
        if not normalized["audience"] or not normalized["goal"] or not normalized["topic"]:
            raise ValueError("audience, goal and topic are required before approval")
        if not normalized["sources"] or not normalized["claims"]:
            raise ValueError("add cited research and at least one sourced claim before approval")
        if any(not claim["source_ids"] for claim in normalized["claims"]):
            raise ValueError("every factual claim needs a source citation")
    now = datetime.now(timezone.utc).isoformat()
    result = {
        **normalized,
        "created_at": existing["created_at"] or now,
        "updated_at": now,
        "review": {"status": "approved", "approved_at": now} if approve else {
            "status": "draft", "approved_at": None,
        },
    }
    result = validate_stored_brief(result, project_id)
    if path.is_symlink():
        raise ValueError("creative brief artifact must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(payload) > MAX_ARTIFACT_BYTES:
        raise ValueError("creative brief artifact exceeds the size limit")
    fd, temporary = tempfile.mkstemp(prefix=".creative-brief-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if path.is_symlink():
            raise ValueError("creative brief artifact must not be a symlink")
        os.replace(temporary, path)
        temporary = ""
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
    return result
