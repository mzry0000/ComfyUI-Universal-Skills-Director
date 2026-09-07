"""Stage-first Director contracts. No media-specific fields on unrelated stages."""

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_serializer

from ..config import DirectorTargetProfile
from ..contracts import Contract, ShortText, Text

Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,79}$")]
Hash = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Label = Annotated[str, StringConstraints(min_length=1, max_length=200, pattern=r"\S")]
Positive = Annotated[int, Field(ge=1)]


class Binding(Contract):
    slot: Literal["image1", "image2", "image3", "image4", "text1", "text2", "text3", "text4"]
    source_type: Literal["input", "stage"]
    source_id: Identifier


class Parameter(Contract):
    name: Identifier
    value: ShortText


class Stage(Contract):
    stage_id: Identifier
    order: Annotated[int, Field(ge=1, le=256)]
    title: Label
    media_kind: Literal["image", "text"]
    target_profile: DirectorTargetProfile
    prompt: Text
    input_bindings: Annotated[list[Binding], Field(max_length=8)]
    parameters: Annotated[list[Parameter], Field(max_length=32)]


class DirectorDraft(Contract):
    title: Label
    summary: ShortText
    stages: Annotated[list[Stage], Field(min_length=1, max_length=256)]
    warnings: Annotated[list[ShortText], Field(max_length=64)]


class InputAsset(Contract):
    input_name: Literal["image1", "image2", "image3", "image4"]
    fingerprint: Hash


class DirectorPlan(DirectorDraft):
    schema_version: Literal["2.0"]
    plan_id: Identifier
    revision: Positive
    plan_signature: Hash
    specification_fingerprint: Hash
    brief_fingerprint: Hash
    assets: Annotated[list[InputAsset], Field(max_length=4)]


class Artifact(Contract):
    kind: Literal["image", "text"]
    fingerprint: Hash
    value: Text
    file_sha256: Hash | None = None

    @model_serializer(mode="wrap")
    def preserve_legacy_shape(self, handler):
        value = handler(self)
        if self.file_sha256 is None:
            value.pop("file_sha256", None)
        return value


class Attempt(Contract):
    number: Positive
    ticket_id: Hash
    status: Literal["running", "succeeded", "failed", "cancelled"]
    execution_hash: Hash
    result: Artifact | None
    error: ShortText | None


class Entry(Contract):
    stage_id: Identifier
    definition_hash: Hash
    attempts: Annotated[list[Attempt], Field(max_length=1000)]


class Ledger(Contract):
    schema_version: Literal["2.0"]
    plan_id: Identifier
    plan_revision: Positive
    plan_signature: Hash
    ledger_revision: Annotated[int, Field(ge=0)]
    entries: Annotated[list[Entry], Field(min_length=1, max_length=256)]


class Ticket(Contract):
    plan_id: Identifier
    plan_revision: Positive
    plan_signature: Hash
    stage_id: Identifier
    attempt: Positive
    execution_hash: Hash
    ticket_id: Hash
