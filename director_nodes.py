"""Optional manual Director v2 nodes; no queue, scheduler, or remote controller."""

from pathlib import Path

from .nodes import PromptComposerNode, _SHARED_CLIENT
from .universal_skills.contracts import canonical_json
from .universal_skills.errors import UniversalSkillHostError
from .universal_skills.director.assets import ArtifactStore, image_fingerprint, text_artifact
from .universal_skills.director.core import DirectorError, IMAGE_TARGETS, TEXT_TARGETS
from .universal_skills.director.planner import DirectorPlanner
from .universal_skills.director.sessions import DirectorSessionStore
from .universal_skills.director.state import (
    checked_ticket,
    record_result,
    resolve_binding,
    select_work_item,
)

PLAN = ("USH2_DIRECTOR_PLAN", {"forceInput": True})
LEDGER = ("USH2_DIRECTOR_LEDGER", {"forceInput": True})
TICKET = ("USH2_DIRECTOR_TICKET", {"forceInput": True})
CATEGORY = "Universal Skills/Director v2"


def _storage_base():
    try:
        import folder_paths

        base = Path(folder_paths.get_output_directory()).resolve()
    except ImportError:
        base = Path(__file__).resolve().parent / "runtime"
        base.mkdir(exist_ok=True)
    return base


def _sessions():
    base = _storage_base()
    return DirectorSessionStore(
        base / "universal_skills_director_v2" / "sessions", trusted_base=base
    )


def _artifacts():
    base = _storage_base()
    return ArtifactStore(base / "universal_skills_director_v2" / "results", trusted_base=base)


class PlanProjectNode:
    CATEGORY = CATEGORY
    FUNCTION = "plan"
    RETURN_TYPES = ("USH2_DIRECTOR_PLAN", "USH2_DIRECTOR_LEDGER", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("plan", "ledger", "plan_json", "ledger_json", "warnings")

    @classmethod
    def INPUT_TYPES(cls):
        inputs = PromptComposerNode.INPUT_TYPES()
        choices, options = inputs["required"]["target_profile"]
        inputs["required"]["target_profile"] = (
            [value for value in choices if value in IMAGE_TARGETS | TEXT_TARGETS],
            options,
        )
        inputs["required"]["max_output_tokens"][1]["default"] = 16384
        inputs["optional"].update(previous_plan=PLAN, previous_ledger=LEDGER)
        return inputs

    def plan(
        self,
        specification,
        request,
        previous_plan=None,
        previous_ledger=None,
        image1=None,
        image2=None,
        image3=None,
        image4=None,
        **settings,
    ):
        plan, ledger = DirectorPlanner(client=_SHARED_CLIENT).plan(
            specification=specification,
            request=request,
            previous_plan=previous_plan,
            previous_ledger=previous_ledger,
            images=list(
                zip(("image1", "image2", "image3", "image4"), (image1, image2, image3, image4))
            ),
            **settings,
        )
        return (
            plan,
            ledger,
            canonical_json(plan),
            canonical_json(ledger),
            "\n".join(plan["warnings"]),
        )


class SelectWorkItemNode:
    CATEGORY = CATEGORY
    FUNCTION = "select"
    RETURN_TYPES = (
        "STRING",
        "USH2_DIRECTOR_LEDGER",
        "USH2_DIRECTOR_TICKET",
        "STRING",
        "STRING",
    )
    RETURN_NAMES = (
        "final_prompt",
        "started_ledger",
        "ticket",
        "bindings_json",
        "parameters_json",
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "plan": PLAN,
                "ledger": LEDGER,
                "stage_id": ("STRING", {"default": "", "dynamicPrompts": False}),
                "mode": (["next_ready", "retry", "resume"], {"default": "next_ready"}),
            }
        }

    def select(self, plan, ledger, stage_id="", mode="next_ready"):
        stage, ticket, started = select_work_item(plan, ledger, stage_id=stage_id, mode=mode)
        return (
            stage["prompt"],
            started,
            ticket,
            canonical_json(stage["input_bindings"]),
            canonical_json(stage["parameters"]),
        )


def _record_inputs(extra):
    return {"required": {"plan": PLAN, "ledger": LEDGER, "ticket": TICKET, **extra}}


def _assert_result_kind(plan, ledger, ticket, kind):
    plan, _, _, _, attempt = checked_ticket(plan, ledger, ticket)
    stage = next(s for s in plan["stages"] if s["stage_id"] == ticket["stage_id"])
    if stage["media_kind"] != kind or attempt["status"] not in {"running", "succeeded"}:
        raise DirectorError("The attempt cannot accept this result kind or status.")
    return attempt


class RecordImageNode:
    CATEGORY = CATEGORY
    FUNCTION = "record"
    OUTPUT_NODE = True
    RETURN_TYPES = ("USH2_DIRECTOR_LEDGER", "IMAGE")
    RETURN_NAMES = ("ledger", "image")

    @classmethod
    def INPUT_TYPES(cls):
        return _record_inputs({"generated_image": ("IMAGE",)})

    def record(self, plan, ledger, ticket, generated_image):
        attempt = _assert_result_kind(plan, ledger, ticket, "image")
        artifact = _artifacts().save_image(generated_image, expected_artifact=attempt["result"])
        return record_result(
            plan, ledger, ticket, status="succeeded", result=artifact
        ), generated_image


class RecordTextNode:
    CATEGORY = CATEGORY
    FUNCTION = "record"
    OUTPUT_NODE = True
    RETURN_TYPES = ("USH2_DIRECTOR_LEDGER", "STRING")
    RETURN_NAMES = ("ledger", "text")

    @classmethod
    def INPUT_TYPES(cls):
        return _record_inputs({"generated_text": ("STRING", {"forceInput": True})})

    def record(self, plan, ledger, ticket, generated_text):
        _assert_result_kind(plan, ledger, ticket, "text")
        return record_result(
            plan, ledger, ticket, status="succeeded", result=text_artifact(generated_text)
        ), generated_text


class RecordFailureNode:
    CATEGORY = CATEGORY
    FUNCTION = "record"
    OUTPUT_NODE = True
    RETURN_TYPES = ("USH2_DIRECTOR_LEDGER",)
    RETURN_NAMES = ("ledger",)

    @classmethod
    def INPUT_TYPES(cls):
        return _record_inputs(
            {
                "status": (["failed", "cancelled"], {"default": "failed"}),
                "reason": (
                    "STRING",
                    {"default": "Generation failed.", "dynamicPrompts": False},
                ),
            }
        )

    def record(self, plan, ledger, ticket, status="failed", reason="Generation failed."):
        return (record_result(plan, ledger, ticket, status=status, error=reason),)


class ResolveImageNode:
    CATEGORY = CATEGORY
    FUNCTION = "resolve"
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)

    @classmethod
    def INPUT_TYPES(cls):
        inputs = _record_inputs(
            {"slot": (["image1", "image2", "image3", "image4"], {"default": "image1"})}
        )
        inputs["optional"] = {f"image{i}": ("IMAGE",) for i in range(1, 5)}
        return inputs

    def resolve(
        self,
        plan,
        ledger,
        ticket,
        slot="image1",
        image1=None,
        image2=None,
        image3=None,
        image4=None,
    ):
        binding = resolve_binding(plan, ledger, ticket, slot)
        if not binding["slot"].startswith("image"):
            raise DirectorError("Resolve Image requires an image slot.")
        if binding["source_type"] == "input":
            image = dict(
                zip(("image1", "image2", "image3", "image4"), (image1, image2, image3, image4))
            )[binding["source_id"]]
            if image is None or image_fingerprint(image) != binding["fingerprint"]:
                raise DirectorError(
                    "Connect the original image used to plan this stage; changed references require replanning."
                )
            return (image,)
        import torch

        return (torch.from_numpy(_artifacts().load_image(binding["result"])),)


class ResolveTextNode:
    CATEGORY = CATEGORY
    FUNCTION = "resolve"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("text",)

    @classmethod
    def INPUT_TYPES(cls):
        return _record_inputs(
            {"slot": (["text1", "text2", "text3", "text4"], {"default": "text1"})}
        )

    def resolve(self, plan, ledger, ticket, slot="text1"):
        binding = resolve_binding(plan, ledger, ticket, slot)
        if (
            not slot.startswith("text")
            or binding["source_type"] != "stage"
            or binding["result"]["kind"] != "text"
        ):
            raise DirectorError("Resolve Text requires a completed text-stage binding.")
        return (binding["result"]["value"],)


class SaveSessionNode:
    CATEGORY = CATEGORY
    FUNCTION = "save"
    OUTPUT_NODE = True
    RETURN_TYPES = ("STRING", "INT")
    RETURN_NAMES = ("snapshot_json", "session_revision")

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "plan": PLAN,
                "ledger": LEDGER,
                "session_name": ("STRING", {"default": "director-session"}),
                "expected_session_revision": (
                    "INT",
                    {"default": -1, "min": -1, "max": 2147483647},
                ),
            }
        }

    def save(self, plan, ledger, session_name="director-session", expected_session_revision=-1):
        snapshot = _sessions().save(
            session_name, plan, ledger, expected_session_revision=expected_session_revision
        )
        return canonical_json(snapshot), snapshot["session_revision"]


class LoadSessionNode:
    CATEGORY = CATEGORY
    FUNCTION = "load"
    RETURN_TYPES = ("USH2_DIRECTOR_PLAN", "USH2_DIRECTOR_LEDGER", "INT", "STRING")
    RETURN_NAMES = ("plan", "ledger", "session_revision", "snapshot_json")

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "session_file": (
                    "STRING",
                    {"default": "director-session.json", "dynamicPrompts": False},
                )
            }
        }

    @classmethod
    def IS_CHANGED(cls, session_file):
        try:
            return _sessions().fingerprint(session_file)
        except UniversalSkillHostError:
            return float("nan")

    def load(self, session_file):
        snapshot = _sessions().load(session_file)
        return (
            snapshot["plan"],
            snapshot["ledger"],
            snapshot["session_revision"],
            canonical_json(snapshot),
        )


NODE_CLASS_MAPPINGS = {
    "USH2_DirectorPlanProject": PlanProjectNode,
    "USH2_DirectorSelectWorkItem": SelectWorkItemNode,
    "USH2_DirectorRecordImage": RecordImageNode,
    "USH2_DirectorRecordText": RecordTextNode,
    "USH2_DirectorRecordFailure": RecordFailureNode,
    "USH2_DirectorResolveImage": ResolveImageNode,
    "USH2_DirectorResolveText": ResolveTextNode,
    "USH2_DirectorSaveSession": SaveSessionNode,
    "USH2_DirectorLoadSession": LoadSessionNode,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    key: "Universal Skills Director v2: " + label
    for key, label in zip(
        NODE_CLASS_MAPPINGS,
        (
            "Plan Project",
            "Select Work Item",
            "Record Image",
            "Record Text",
            "Record Failure",
            "Resolve Image Input",
            "Resolve Text Input",
            "Save Session",
            "Load Session",
        ),
    )
}
