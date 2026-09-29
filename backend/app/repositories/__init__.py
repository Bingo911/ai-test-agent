"""Tenant-scoped repositories: the only place raw table access lives (§14.2)."""

from .artifacts import ArtifactRepository, ElementMemoryRepository, FailureAnalysisRepository
from .base import Scoped, json_copy
from .cases import CaseRepository, CompileRepository, TagRepository, source_digest
from .executions import ExecutionRepository
from .human import CommandRepository, HumanTaskRepository
from .outbox import OutboxRepository, outbox_dedupe_key
from .platform import AccessRepository, AuditRepository, IdempotencyRepository, request_digest
from .reservations import PoolRepository, ReservationRepository, WorkerLeaseRepository
from .resources import AttachmentRepository, EnvironmentRepository, revision_digest

__all__ = [
    "AccessRepository",
    "ArtifactRepository",
    "AttachmentRepository",
    "AuditRepository",
    "CaseRepository",
    "CommandRepository",
    "CompileRepository",
    "ElementMemoryRepository",
    "EnvironmentRepository",
    "ExecutionRepository",
    "FailureAnalysisRepository",
    "HumanTaskRepository",
    "IdempotencyRepository",
    "OutboxRepository",
    "PoolRepository",
    "ReservationRepository",
    "Scoped",
    "TagRepository",
    "WorkerLeaseRepository",
    "json_copy",
    "outbox_dedupe_key",
    "request_digest",
    "revision_digest",
    "source_digest",
]
