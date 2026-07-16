from __future__ import annotations

from app.db.calls import CallsMixin
from app.db.deployment import DEPLOYMENT_LOCK_TTL, DeploymentLockedError, DeploymentMixin
from app.db.engine import DatabaseEngine
from app.db.plans import PlansMixin
from app.db.questions import QuestionsMixin
from app.db.telemetry import LatencyMark, LatencyStage, TelemetryMixin
from app.db.termination import TerminationMixin
from app.db.transcripts import TranscriptsMixin
from app.db.transfers import TRANSFER_ELIGIBLE_STATES, TransfersMixin
from app.db.webhooks import WebhooksMixin

__all__ = [
    "DEPLOYMENT_LOCK_TTL",
    "TRANSFER_ELIGIBLE_STATES",
    "Database",
    "DeploymentLockedError",
    "LatencyMark",
    "LatencyStage",
]


class Database(
    PlansMixin,
    DeploymentMixin,
    CallsMixin,
    TransfersMixin,
    TerminationMixin,
    TelemetryMixin,
    WebhooksMixin,
    TranscriptsMixin,
    QuestionsMixin,
    DatabaseEngine,
):
    """Composed SQLite data-access facade; see app/db/engine.py and the per-concern mixins."""
