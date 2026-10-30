"""Strict content-free ownership metadata and archive-first atomic commits."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

HOT_TERMINAL_LIMIT = 1000


class StoreError(RuntimeError):
    """Fixed content-free persistence failure."""


def validate_payload(payload, conversation_url):
    """Validate the exact active/archive schema and migrate detached v1 records."""
    if (
        not isinstance(payload, dict)
        or set(payload) != {"version", "sessions"}
        or type(payload["version"]) is not int
        or payload["version"] not in (1, 2)
        or not isinstance(payload["sessions"], dict)
    ):
        raise ValueError
    fields = {
        "task_id",
        "created_at",
        "conversation_id",
        "url",
        "keep",
        "answer_result",
        "cleanup",
        "submission",
    }
    extra = {
        "cleanup_phase",
        "cleanup_reason",
        "cleanup_updated_at",
        "cleanup_resolution",
    }
    for task_id, record in payload["sessions"].items():
        if (
            not isinstance(task_id, str)
            or not task_id
            or len(task_id) > 200
            or not isinstance(record, dict)
            or set(record) != (fields if payload["version"] == 1 else fields | extra)
            or record["task_id"] != task_id
            or type(record["keep"]) is not bool
            or record["submission"] not in ("not_attempted", "attempted", "confirmed")
            or record["answer_result"]
            not in ("pending", "success", "failed", "cancelled", "timed_out")
            or record["cleanup"]
            not in (
                "pending",
                "not_created",
                "retained",
                "deleted",
                "cleanup_failed",
                "cleanup_unknown",
            )
            or datetime.fromisoformat(record["created_at"]).tzinfo is None
        ):
            raise ValueError
        conversation_id = record["conversation_id"]
        if conversation_id is None:
            if record["url"] is not None:
                raise ValueError
        elif (
            not isinstance(conversation_id, str)
            or record["submission"] != "confirmed"
            or record["url"] != conversation_url(conversation_id)
        ):
            raise ValueError
        if payload["version"] == 1:
            record.update(
                cleanup_phase=(
                    "not_required"
                    if record["keep"]
                    or record["cleanup"] in ("retained", "not_created")
                    else "confirmed"
                    if record["cleanup"] == "deleted"
                    else "legacy_unknown"
                ),
                cleanup_reason="legacy_record",
                cleanup_updated_at=record["created_at"],
                cleanup_resolution=None,
            )
        if (
            record["cleanup_phase"]
            not in (
                "not_required",
                "not_started",
                "delete_requested",
                "confirmed",
                "legacy_unknown",
            )
            or not isinstance(record["cleanup_reason"], str)
            or re.fullmatch(r"[a-z_]{0,80}", record["cleanup_reason"]) is None
            or datetime.fromisoformat(record["cleanup_updated_at"]).tzinfo is None
            or (
                record["cleanup_phase"] == "not_required"
                and not record["keep"]
                and record["cleanup"] != "not_created"
            )
            or (
                record["cleanup"] == "deleted"
                and record["cleanup_phase"] != "confirmed"
            )
        ):
            raise ValueError
        resolution = record["cleanup_resolution"]
        if resolution is not None and (
            not isinstance(resolution, dict)
            or set(resolution) != {"actor_id", "resolved_at", "action"}
            or resolution["action"] != "acknowledged"
            or not isinstance(resolution["actor_id"], str)
            or re.fullmatch(r"[0-9]{1,20}", resolution["actor_id"]) is None
            or not any(c != "0" for c in resolution["actor_id"])
            or datetime.fromisoformat(resolution["resolved_at"]).tzinfo is None
        ):
            raise ValueError
    return payload["sessions"]


def archivable(record):
    """Only definitive terminal records without retention or resolution qualify."""
    return (
        record["keep"] is False
        and record["cleanup_resolution"] is None
        and record["answer_result"] != "pending"
        and (
            (record["cleanup"] == "deleted" and record["cleanup_phase"] == "confirmed")
            or (
                record["cleanup"] == "not_created"
                and record["cleanup_phase"] == "not_required"
            )
        )
    )


class SessionStore:
    """Use only detached snapshots; caller serializes physical writes."""

    def __init__(self, root: Path, registry: Path, conversation_url):
        self.root = root.resolve()
        self.registry = registry
        self.archive = registry.with_name(registry.stem + ".archive")
        self._archive_namespace = registry.parent.resolve() / self.archive.name
        self.conversation_url = conversation_url

    def _checked(self, path):
        is_archive = path.parent == self.archive
        try:
            resolved = path.resolve()
            namespace = self.archive.resolve() if is_archive else None
        except (OSError, RuntimeError):
            raise StoreError("metadata_path") from None
        if (
            resolved == self.root
            or not resolved.is_relative_to(self.root)
            or (
                is_archive
                and (
                    namespace != self._archive_namespace
                    or resolved != self._archive_namespace / path.name
                )
            )
        ):
            raise StoreError("metadata_path")
        return path

    def archive_path(self, task_id):
        """Hash exact UTF-8 IDs and enforce the registry-specific namespace."""
        try:
            digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()
        except UnicodeEncodeError:
            raise StoreError("metadata_invalid") from None
        return self._checked(self.archive / (digest + ".json"))

    def _decode(self, raw):
        try:
            payload = json.loads(raw.decode("utf-8"))
            records = validate_payload(payload, self.conversation_url)
            return payload["version"], records
        except Exception:
            raise StoreError("metadata_invalid") from None

    def load(self):
        """Read active metadata only, preserving original v1 backup bytes."""
        try:
            self._checked(self.registry)
            raw = self.registry.read_bytes()
        except FileNotFoundError:
            return {}, None
        except OSError:
            raise StoreError("metadata_read") from None
        version, records = self._decode(raw)
        return records, raw if version == 1 else None

    def lookup(self, task_id):
        """Read one exact final archive without scanning the archive directory."""
        try:
            raw = self.archive_path(task_id).read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            raise StoreError("metadata_read") from None
        version, records = self._decode(raw)
        if (
            version != 2
            or set(records) != {task_id}
            or not archivable(records[task_id])
        ):
            raise StoreError("archive_invalid")
        return records[task_id]

    def _atomic(self, path, payload):
        temporary = None
        try:
            self._checked(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=".owned_sessions-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
            self._checked(path)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def commit(self, records, protected, migration_source):
        """Archive oldest eligible excess records before committing active metadata.

        Args:
            records: Detached metadata snapshot, never the browser's live dict.
            protected: Snapshot of IDs with active resource ownership.
            migration_source: Exact original v1 bytes, if a backup is required.

        Returns:
            Archived snapshots safe for conditional in-memory pruning after commit.

        Raises:
            StoreError: No live record may be removed after any failed write.
        """
        try:
            self._checked(self.registry)
            self.registry.parent.mkdir(parents=True, exist_ok=True)
            if migration_source is not None:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=self.registry.parent,
                    prefix=self.registry.name + ".v1-backup-",
                    suffix=".json",
                    delete=False,
                ) as backup:
                    backup.write(migration_source)
                    backup.flush()
                    os.fsync(backup.fileno())
            eligible = sorted(
                (
                    key
                    for key, record in records.items()
                    if key not in protected and archivable(record)
                ),
                key=lambda key: (
                    datetime.fromisoformat(records[key]["created_at"]),
                    key,
                ),
            )
            archived = {
                key: records[key]
                for key in eligible[: max(0, len(eligible) - HOT_TERMINAL_LIMIT)]
            }
            for key, record in archived.items():
                # Validate even new snapshots before they can become durable archives.
                payload = {"version": 2, "sessions": {key: record}}
                validate_payload(payload, self.conversation_url)
                previous = self.lookup(key)
                if previous is not None:
                    if previous != record:
                        raise StoreError("archive_conflict")
                else:
                    self._atomic(self.archive_path(key), payload)
            self._atomic(
                self.registry,
                {
                    "version": 2,
                    "sessions": {
                        key: record
                        for key, record in records.items()
                        if key not in archived
                    },
                },
            )
            return archived
        except Exception:
            raise StoreError("metadata_write") from None
