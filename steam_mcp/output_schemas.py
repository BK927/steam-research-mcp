"""Published success contracts; CallToolResult preserves the original wire payload.

Provider/view-specific data stays extensible. The stable envelope, pagination,
provenance and job handles are typed so clients can validate and follow results.
Tool execution errors remain separate MCP isError results.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class OutputObject(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ResultPage(OutputObject):
    returned: int = Field(ge=0, le=100)
    has_more: bool
    next_cursor: str | None


class ResultMeta(OutputObject):
    source: str
    provider: str
    retrieved_at: str
    fresh_until: str | None
    quota_cost: int | float | None
    canonical_uri: str | None
    warnings: list[str]
    untrusted_fields: list[str]


class ReadOutput(OutputObject):
    schema_version: Literal["1"]
    kind: Literal["entity", "collection"]
    data: dict[str, Any] = Field(description="View-specific fields and collection aggregates.")
    items: list[Any] = Field(max_length=100, description="One page of view-specific results.")
    job: OutputObject
    page: ResultPage
    meta: ResultMeta


class AnalysisJob(OutputObject):
    job_id: str
    task: str
    status: Literal["queued", "running", "succeeded", "failed", "cancel_requested", "cancelled"]
    progress: dict[str, Any]
    error: dict[str, Any] | None
    status_uri: str
    result_uri: str | None
    created_at: str
    updated_at: str
    expires_at: str
    effective_limits: dict[str, int | float] | None = None


class JobOutput(ReadOutput):
    kind: Literal["job"]
    job: AnalysisJob


class CancellationData(OutputObject):
    cancelled: bool
    cancel_requested: bool
    reason: Literal["already_terminal", "cancellation_requested"]


class CancelOutput(JobOutput):
    data: CancellationData
