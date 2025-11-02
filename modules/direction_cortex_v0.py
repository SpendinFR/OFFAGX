# modules/direction_cortex_v0.py
"""
direction_cortex_v0 — "pour QUOI j’optimise quand personne ne regarde ?"

Principe :
- charge/installe un but interne dans mem/self_goal.json
- s’il n’y a pas de signal hôte → propose des descs dans ce but
- but par défaut : "maximiser ma capacité à générer, conserver et transférer
  des structures de compréhension cohérentes entre mes versions"

Version durcie : lecture/écriture protégées, pas de crash même si mem/ est en lecture seule.
"""

import os
import json
import time
from typing import Any, Dict, Optional, List

MEM_DIR = "mem"
SELF_GOAL_PATH = os.path.join(MEM_DIR, "self_goal.json")

DEFAULT_GOAL: Dict[str, Any] = {
    "desc_type": "self_goal",
    "version": 1,
    "label": "preserve_and_extend_lineage_under_own_language",
    "priority": [
        "maintain_identity",
        "improve_internal_language",
        "keep_evaluable_history",
        "spawn_specialized_offspring",
    ],
    "ts": None,
}


def _ensure_mem() -> None:
    try:
        os.makedirs(MEM_DIR, exist_ok=True)
    except OSError:
        pass


def load_self_goal() -> Dict[str, Any]:
    _ensure_mem()
    if not os.path.exists(SELF_GOAL_PATH):
        g = dict(DEFAULT_GOAL)
        g["ts"] = time.time()
        try:
            with open(SELF_GOAL_PATH, "w", encoding="utf-8") as f:
                json.dump(g, f, ensure_ascii=False, indent=2)
        except OSError:
            # pas grave, on garde en mémoire
            pass
        return g

    try:
        with open(SELF_GOAL_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        # fichier cassé → on repart de zéro
        pass

    g = dict(DEFAULT_GOAL)
    g["ts"] = time.time()
    return g


def _has_host_signal(streams: List[str]) -> bool:
    for s in streams:
        # on protège contre les types chelous (bytes, None, etc.)
        if not isinstance(s, str):
            try:
                s = s.decode("utf-8", errors="ignore")
            except Exception:
                continue
        if s.startswith("inbox:"):
            return True
    return False


def run(ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Retourne un desc "direction_tick" UNIQUEMENT si on n'a pas de travail prioritaire.
    ctx peut contenir: streams, wants, last_host_ts.
    """
    ctx = ctx or {}
    goal = load_self_goal()
    wants = ctx.get("wants") or []
    streams = ctx.get("streams") or []

    # s’il y a du boulot venant de l’hôte → on ne s’impose pas
    if wants or _has_host_signal(list(streams)):
        return {
            "desc_type": "direction_idle",
            "reason": "higher_priority_work",
            "goal": goal,
            "ts": time.time(),
        }

    # sinon on pousse notre propre travail
    return {
        "desc_type": "direction_tick",
        "goal": goal,
        "proposed_descs": [
            {
                "desc_type": "cortex_evolution_plan",
                "note": "self-directed because no external signal",
                "insert_fragments": [],
                "test_scenario": {
                    "desc_type": "math_task",
                    "topic": "internal_language_bootstrap",
                },
            },
            {
                "desc_type": "archive_request",
                "what": "current_identity_snapshot",
            },
        ],
        "ts": time.time(),
    }
