"""Population diversity across lineage and strategy dimensions (DESIGN §10.4)."""

from __future__ import annotations

import math
from collections import Counter
from typing import Any, Iterable


def entropy(values: Iterable[Any]) -> float:
    counts = Counter(values)
    total = sum(counts.values())
    if not total:
        return 0.0
    return -sum((c / total) * math.log(c / total) for c in counts.values())


def diversity_report(agents: Iterable[Any]) -> dict[str, Any]:
    agents = list(agents)
    lineages = Counter(a.lineage_id for a in agents)
    segments = Counter(a.genotype["target"]["segment"] for a in agents)
    workflows = Counter(a.genotype["workflow"] for a in agents)
    dominant, dominant_n = (lineages.most_common(1)[0] if lineages else (None, 0))
    h = entropy(a.lineage_id for a in agents)
    return {
        "population": len(agents),
        "lineages": len(lineages),
        "lineage_entropy": round(h, 6),
        "effective_lineages": round(math.exp(h), 6),
        "segment_entropy": round(entropy(a.genotype["target"]["segment"] for a in agents), 6),
        "workflow_entropy": round(entropy(a.genotype["workflow"] for a in agents), 6),
        "dominant_lineage": dominant,
        "dominant_lineage_share": round(dominant_n / len(agents), 6) if agents else 0.0,
        "segments": dict(sorted(segments.items())),
        "workflows": dict(sorted(workflows.items())),
    }
