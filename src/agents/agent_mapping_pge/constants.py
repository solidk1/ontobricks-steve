"""Constants shared by the PGE sub-agents.

Only values that are genuinely identical across the Planner, Generators and
Critic live here. The per-stage knobs — ``MAX_ITERATIONS``, ``LLM_TIMEOUT``,
``_ITERATION_DELAY_SEC``, ``SYSTEM_PROMPT``, ``_TRACE_NAME`` — deliberately stay
in each module, because they differ per stage: the Planner iterates up to 50
times, the Critic 6, and hoisting them would silently retune every stage.
"""

# The submit_* JSON for a real-world ontology can run several KB (17+ classes ×
# multiple candidates + canonical_ids + join_keys + plan). A small ceiling
# silently truncates the call (finish_reason=length) and the dataclass validation
# then fails with no clue to the LLM as to why. This removes the practical
# ceiling for any ontology size; you only pay for tokens actually generated, so
# cost stays bounded by output complexity.
#
# All four PGE stages need the same headroom for the same reason. Three of them
# used to carry a copy plus a comment reading "See planner._MAX_TOKENS comment —
# same rationale", which is the cross-reference-by-comment that this module
# replaces.
MAX_TOKENS = 50000
