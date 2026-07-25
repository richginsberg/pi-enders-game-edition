"""dnc-burst — rent a GPU, saturate it for hours, give it back before it costs you."""
from .policy import Decision, Demand, Economics, Policy, Verdict, should_burst
from .profiles import GPUS, MODELS, CapacityPlan, plan_capacity, rank_gpus
from .template import RenderedTemplate, best_template, render

__all__ = [
    "Decision", "Demand", "Economics", "Policy", "Verdict", "should_burst",
    "GPUS", "MODELS", "CapacityPlan", "plan_capacity", "rank_gpus",
    "RenderedTemplate", "render", "best_template",
]
__version__ = "0.1.0"
