"""Eligibility gate, fitness evaluation, and causal attribution."""

from .attribution import causal_report
from .eligibility import Eligibility, evaluate_eligibility
from .fitness import DISQUALIFIED, FitnessEvaluator, FitnessResult
from .stats import Posterior, shrink, spearman

__all__ = [
    "DISQUALIFIED", "Eligibility", "FitnessEvaluator", "FitnessResult", "Posterior", "causal_report",
    "evaluate_eligibility", "shrink", "spearman",
]
