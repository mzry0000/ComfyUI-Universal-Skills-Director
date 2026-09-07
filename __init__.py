"""Small default node set, with an explicit opt-in Director package."""

from .nodes import LoadSpecificationNode, PromptComposerNode, PromptPlannerNode
from .universal_skills.local_config import load_local_config

NODE_CLASS_MAPPINGS = {
    "USH_LoadSpecification": LoadSpecificationNode,
    "USH_PromptComposer": PromptComposerNode,
    "USH_PromptPlanner": PromptPlannerNode,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "USH_LoadSpecification": "Universal Skills: Load Skill",
    "USH_PromptComposer": "Universal Skills: Prompt Composer",
    "USH_PromptPlanner": "Universal Skills: Prompt Planner (legacy)",
}

if load_local_config().enable_director:
    from .director_nodes import NODE_CLASS_MAPPINGS as DIRECTOR_CLASSES
    from .director_nodes import NODE_DISPLAY_NAME_MAPPINGS as DIRECTOR_NAMES

    NODE_CLASS_MAPPINGS.update(DIRECTOR_CLASSES)
    NODE_DISPLAY_NAME_MAPPINGS.update(DIRECTOR_NAMES)

WEB_DIRECTORY = "./web/js"
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
