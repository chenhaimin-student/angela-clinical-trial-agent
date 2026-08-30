"""Offline, evidence-backed quality evaluation for Angela's CDE retrieval."""

from .contracts import EvaluationCase, EvaluationSuite, load_suite
from .runner import QualityEvaluator

__all__ = ["EvaluationCase", "EvaluationSuite", "QualityEvaluator", "load_suite"]
