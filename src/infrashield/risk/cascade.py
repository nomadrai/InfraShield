"""Cascading impact on the dependency graph.

Reuses the existing, tested ``cascade_from`` weighted propagation from
``infrashield.graph.dependencies`` (kept after algorithm review — see
docs/algorithm.md) and converts its downstream-impact dict into per-asset
impact components: cascade impact, depth, and dependent counts."""
from __future__ import annotations

import heapq

import networkx as nx


def compute_cascade_impacts(
    graph: nx.DiGraph,
    risk_by_asset: dict[str, float],
    fail_risk: float = 0.45,
) -> dict[str, dict]:
    """Propagate failure of at-risk assets through the dependency graph.

    Multi-source propagation (one reverse-BFS over the *union* of all
    failing assets) so cost is O(V+E) regardless of how many assets fail,
    instead of one cascade per failing asset:

    * ``cascade``: max over failing assets of weighted propagation strength
      (edge weight product along the path, same semantics as
      ``cascade_from``)
    * ``depth``: BFS hops from the nearest failing asset
    * ``dependents``: number of distinct failing assets upstream
    """
    import collections

    failing = [a for a, r in risk_by_asset.items() if r >= fail_risk and a in graph]
    if not failing:
        return {}

    rev = graph.reverse()
    best: dict[str, float] = {}
    depth: dict[str, int] = {a: 0 for a in failing}
    dependents: dict[str, int] = {}

    # Dijkstra-style max-strength propagation with relaxation pruning: a node
    # is only re-enqueued when a strictly stronger signal arrives, so the
    # queue is bounded by O(E) regardless of path count.
    heap: list[tuple[float, int, str]] = []  # (-strength, hops, node) max-first
    for a in failing:
        heapq.heappush(heap, (-1.0, 0, a))
    while heap:
        neg, hops, node = heapq.heappop(heap)
        strength = -neg
        if strength < best.get(node, 0.0) and node in best:
            continue  # stale entry (already propagated stronger)
        # NB: out_edges yields (source, target, data); in the reversed graph
        # the TARGET is the dependent that feels the failure.
        for _, dependent, data in rev.out_edges(node, data=True):
            w = strength * float(data.get("weight", 1.0))
            if w < 0.05:  # same decay floor as cascade_from
                continue
            if w > best.get(dependent, 0.0):
                best[dependent] = w
                depth[dependent] = min(depth.get(dependent, 99), hops + 1)
                dependents[dependent] = dependents.get(dependent, 0) + 1
                if hops + 1 <= 8:  # bounded propagation depth
                    heapq.heappush(heap, (-w, hops + 1, dependent))

    results: dict[str, dict] = {}
    for node, val in best.items():
        results[node] = {
            "cascade": round(val, 3),
            "depth": depth.get(node, 99),
            "dependents": dependents.get(node, 1),
        }
    # failing assets themselves: they are the impact sources
    for a in failing:
        results[a] = {
            "cascade": 1.0, "depth": 0,
            "dependents": graph.in_degree(a),
        }
    return results
