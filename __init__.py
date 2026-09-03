"""ComfyUI registration for local Specification and Director nodes."""

from .director_nodes import (
    DirectorLoadSessionNode,
    DirectorPlanProjectNode,
    DirectorRecordResultNode,
    DirectorSaveSessionNode,
    DirectorSelectWorkItemNode,
    DirectorTimelineManifestNode,
    DirectorValidatePlanNode,
)
from .nodes import (
    LoadSpecificationNode,
    PromptPlannerNode,
)


NODE_CLASS_MAPPINGS = {
    "USH_LoadSpecification": LoadSpecificationNode,
    "USH_PromptPlanner": PromptPlannerNode,
    "USH_DirectorPlanProject": DirectorPlanProjectNode,
    "USH_DirectorValidatePlan": DirectorValidatePlanNode,
    "USH_DirectorSelectWorkItem": DirectorSelectWorkItemNode,
    "USH_DirectorRecordResult": DirectorRecordResultNode,
    "USH_DirectorLoadSession": DirectorLoadSessionNode,
    "USH_DirectorSaveSession": DirectorSaveSessionNode,
    "USH_DirectorTimelineManifest": DirectorTimelineManifestNode,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "USH_LoadSpecification": "Universal Skills: Load Specification",
    "USH_PromptPlanner": "Universal Skills: Prompt Planner",
    "USH_DirectorPlanProject": "Universal Skills Director: Plan Project",
    "USH_DirectorValidatePlan": "Universal Skills Director: Validate Plan",
    "USH_DirectorSelectWorkItem": "Universal Skills Director: Select Work Item",
    "USH_DirectorRecordResult": "Universal Skills Director: Record Result",
    "USH_DirectorLoadSession": "Universal Skills Director: Load Session",
    "USH_DirectorSaveSession": "Universal Skills Director: Save Session",
    "USH_DirectorTimelineManifest": "Universal Skills Director: Timeline Manifest",
}

WEB_DIRECTORY = "./web/js"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
