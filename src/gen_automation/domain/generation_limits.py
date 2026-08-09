from gen_automation.domain.controlled_duo import LEGACY_REGIONAL_PROMPT_NODE_CLASSES

MAX_OUTPUTS_PER_GENERATION_JOB = 25

# The signed worker envelope is capped at 256 KiB. Multi-output execution
# duplicates prompt-bearing workflow branches and upload grants inside that
# envelope, so reserve most of the budget for graph structure and grants. This
# bound is checked both on frozen source prompts and again after wildcard
# expansion.
MAX_PROMPT_TEXT_BYTES_PER_GENERATION_JOB = 96 * 1024

# Backward-compatible public name used by the v1 workflow and its tests.
REGIONAL_PROMPT_NODE_CLASSES = LEGACY_REGIONAL_PROMPT_NODE_CLASSES
