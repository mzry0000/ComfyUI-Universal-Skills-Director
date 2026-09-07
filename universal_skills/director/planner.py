"""Optional multi-stage planning through the same mocked Responses boundary."""

from ..composer import (
    PROMPT_WRITING_RULES,
    image_reference_warnings,
    merge_warnings,
    prepare_request,
    structured_payload,
)
from ..contracts import fingerprint, parse_response
from ..openai_client import OpenAIResponsesClient
from ..specification import specification_signature_payload
from .assets import image_fingerprint
from .core import (
    DirectorError,
    IMAGE_TARGETS,
    TEXT_TARGETS,
    hydrate_plan,
    validate_director_plan,
)
from .models import DirectorDraft
from .state import create_ledger, reconcile_ledger, validate_ledger_for_plan

DIRECTOR_INSTRUCTIONS = """Create or revise a small production plan with independent image/text stages.
Return the DirectorDraft JSON, not a ComposerResponse. Each stage.prompt is its own
complete downstream prompt: image instructions for image stages, writing instructions
for text stages. Do not substitute a keyframe description for a writing task. Apply
the same brevity and explicit-constraint rules below to each stage.prompt.

The Director is manually executed. Do not plan video/audio generation, tools, file
paths, queue controllers or status updates. Use only the provided image inputs and
outputs of other stages. input_bindings map local image1..image4/text1..text4 slots to
either a provided image input or another stage. Name those local slots in the prompt
where needed. Text from dependency stages must be connected separately by the user;
it is not automatically interpolated into prompt text. Do not embed guessed outputs.

For a new stage use a unique temporary stage_id starting with new-. For revisions,
retain the exact stage_id of each existing stage, even when reordered or edited.
Keep unaffected stages unchanged; apply the current request as the change request.
Remove a stage only when requested or necessary for the requested change. New IDs
are allocated by the host. order controls display order only. Dependencies must be
acyclic. Each stage has its own prompt, target profile, input bindings and parameters.
Use image profiles for image stages and text_generation/structured_json for text.
parameters are explicit generator controls (for example size or seed), not a second
copy of prose already in the prompt. They are exposed as JSON, not applied to a node.
Do not put planning titles, summaries, QA checks or execution status in prompts.
"""


class DirectorPlanner:
    def __init__(self, *, client=None):
        self.client = client or OpenAIResponsesClient()

    def plan(self, *, previous_plan=None, previous_ledger=None, **kwargs):
        if (previous_plan is None) != (previous_ledger is None):
            raise DirectorError("Connect previous_plan and previous_ledger together.")
        previous = validate_director_plan(previous_plan) if previous_plan is not None else None
        if previous is not None:
            validate_ledger_for_plan(previous, previous_ledger)
        prepared = prepare_request(**kwargs)
        if prepared.data["target_profile"] not in IMAGE_TARGETS | TEXT_TARGETS:
            raise DirectorError(
                "Director v2 supports image/text stages only. Use Prompt Composer for standalone video prompts."
            )
        assets = [
            {"input_name": name, "fingerprint": image_fingerprint(value)}
            for name, value in kwargs.get("images", ())
            if value is not None
        ]
        previous_draft = (
            {key: previous[key] for key in DirectorDraft.model_fields} if previous else None
        )
        payload = structured_payload(
            prepared,
            DirectorDraft,
            DIRECTOR_INSTRUCTIONS + "\n" + PROMPT_WRITING_RULES,
            extra_data={"previous_plan": previous_draft},
        )
        draft = parse_response(
            DirectorDraft, self.client.create_text_response(payload)
        ).model_dump()
        reference_warnings = [
            warning
            for stage in draft["stages"]
            for warning in image_reference_warnings(
                stage["prompt"],
                [binding["slot"] for binding in stage["input_bindings"]],
                location=f"Stage {stage['order']} prompt",
            )
        ]
        draft["warnings"] = list(
            merge_warnings(prepared.warnings, reference_warnings, draft["warnings"])
        )
        plan = hydrate_plan(
            draft,
            assets=assets,
            specification_fingerprint=fingerprint(
                specification_signature_payload(prepared.specification)
            ),
            brief_fingerprint=fingerprint({**prepared.data, "assets": assets}),
            previous_plan=previous,
        )
        ledger = (
            reconcile_ledger(previous, previous_ledger, plan)
            if previous
            else create_ledger(plan)
        )
        return plan, ledger
