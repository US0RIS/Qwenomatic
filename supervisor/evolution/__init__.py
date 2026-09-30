"""Evolution engine: selection, bounded mutation, lineage, generation close."""

from .diversity import diversity_report
from .generation import CLOSE_STEPS, GenerationManager, SimulatedCrash, evaluate_generation, selection_for, snapshot_state
from .lineage import ancestry, lineage_tree
from .mutation import MutationDiff, MutationEngine, MutationError, MutationOperator, MutationRegistry, default_registry
from .selection import plan_selection

__all__ = [
    "CLOSE_STEPS", "GenerationManager", "MutationDiff", "MutationEngine", "MutationError", "MutationOperator",
    "MutationRegistry", "SimulatedCrash", "ancestry", "default_registry", "diversity_report", "evaluate_generation",
    "lineage_tree", "plan_selection", "selection_for", "snapshot_state",
]
