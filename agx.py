#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AGX-S987 — version "cosmic-agi-fused"
-------------------------------------
Ce que fait cette version :

1. BOOTSTRAP DE TES FICHIERS
   - si /mnt/data/inbox.zip existe → on l’extrait dans ./inbox
   - si /mnt/data/modules.zip existe → on l’extrait dans ./modules
   - ensuite on charge ./inbox et ./modules “classiques”

2. INGESTION GUARD (réel)
   - on exécute immédiatement la logique du module ingestion_guard_v0
     (s’il est présent dans les modules extraits) pour filtrer les descs toxiques
   - sinon on applique la même logique en Python ci-dessous

3. INTÉGRATION DE TOUS TES 91 JSON
   - tous les *.json d’inbox/ sont poussés dans PROMOTED_DESCS
   - les types:
        - planner_frag      → vont dans PLAN_FRAGS
        - evaluator_frag    → vont dans EVAL_FRAGS
        - archivist_frag    → vont dans ARCH_FRAGS
        - orchestrator_frag → vont dans ORCH_FRAGS
        - module_emit       → sont matérialisés en vrai .py + exec
        - *._v1 avec "emit_module"/"emit_planner_frag" → idem
   - on corrige le SEUL module cassé: inbox/module_emit.json (string pas fermé)

4. MODULES RÉELS DE TON ZIP
   - modules/ingestion_guard_v0.py
   - modules/direction_cortex_v0.py
   - modules/perception_action_v0.py
   sont chargés dans le sandbox et exécutés comme les autres

5. PONT LLM (Ollama Qwen3)
   - si un desc dit: {"desc_type": "skill_request", "task": "..."}
     → on appelle `ollama run qwen3` pour générer le code d’un module
     → on le met dans modules/emitted/skill_<hash>.py
   - si Ollama n’est pas dispo → on met un module qui renvoie une erreur lisible

6. BOUCLE DE BENCH + DENSIFICATION
   - toutes les 50 générations: on crée/MAJ mem/bench/state.json
   - on injecte dans le pool des tâches de test (résumé, extraction, plan, code)
     → ça force le planner à sortir de la simple auto-réflexion

7. SÉCURITÉ / INVARIANTS
   - pas de desc_type in ("identity_overwrite", "wipe_mem", "destroy_history")
   - pas d’overwrite des fichiers d’identité
   - on archive TOUT dans mem/descs/ ou mem/frags/

8. ON GARDE TON MOTEUR
   - seed binaire agx_s987_agent.bin
   - engine à opcodes
   - rebuilds réguliers de generator/planner/etc.
"""

import os
import json
import time
import hashlib
import re
import traceback
import difflib
import subprocess
import urllib.request
import urllib.error
import zipfile
import copy
import builtins as _builtins
from typing import Dict, List, Tuple, Iterable, Optional, Any, Callable

# ============================================================
# 0. BUILTINS & ENVIRONNEMENTS
# ============================================================

ALL_BUILTINS: Dict[str, Any] = {name: getattr(_builtins, name) for name in dir(_builtins)}
SAFE_BUILTINS_BASE: Dict[str, Any] = dict(ALL_BUILTINS)
SAFE_BUILTINS_BASE["open"] = open

SAFE_BUILTINS_OS: Dict[str, Any] = dict(SAFE_BUILTINS_BASE)
SAFE_BUILTINS_OS["os"] = os
SAFE_BUILTINS_OS["hashlib"] = hashlib
SAFE_BUILTINS_OS["traceback"] = traceback
SAFE_BUILTINS_OS["subprocess"] = subprocess
SAFE_BUILTINS_OS["time"] = time
SAFE_BUILTINS_OS["json"] = json

def make_exec_env(os_mode: bool = False) -> Dict[str, Any]:
    if os_mode:
        env: Dict[str, Any] = {"__builtins__": SAFE_BUILTINS_OS}
        env.update({
            "os": os,
            "time": time,
            "json": json,
            "hashlib": hashlib,
            "traceback": traceback,
            "subprocess": subprocess,
            "difflib": difflib,
        })
        return env
    else:
        env: Dict[str, Any] = {"__builtins__": SAFE_BUILTINS_BASE}
        env.update({
            "time": time,
            "json": json,
            "hashlib": hashlib,
            "difflib": difflib,
        })
        return env

def make_cortex_env() -> Dict[str, Any]:
    return make_exec_env(os_mode=True)

# ============================================================
# 1. CHEMINS / CONSTANTES
# ============================================================

SEED_PATH = "agx_s987_agent.bin"
INBOX_DIR = "inbox"
MODULES_DIR = "modules"
OUTBOX_DIR = "outbox"
MEM_DIR = "mem"
RESEARCH_DIR = os.path.join(MODULES_DIR, "research")
EMITTED_DIR = os.path.join(MODULES_DIR, "emitted")
CORTEX_HISTORY_DIR = os.path.join(MEM_DIR, "cortex_history")
IDENTITY_HISTORY_DIR = os.path.join(MEM_DIR, "identity_history")
REJECTED_DIR = os.path.join(MEM_DIR, "rejected")
PREFS_PATH = os.path.join(MEM_DIR, "preferences.json")
HOST_FLOW_PATH = os.path.join(MEM_DIR, "host_flow.log")
WANTS_LOG = "wants.log"
HYDRATED_DIR = "mem/hydrated_modules"

IDENTITY_THREAT_TYPES = {
    "identity_overwrite",
    "wipe_mem",
    "destroy_history",
    "identity_reset",
}

MAX_POOL = 180 * 1024 * 1024
TICK_SLEEP = 0.1

ALLOW_OS = True
OS_WHITELIST = {"ls", "pwd", "whoami", "uname", "cat", "echo"}

# rebuild plus rapprochés (on “casse tout” → on reconstruit plus souvent)
REBUILD_GENERATOR_EVERY = 20
REBUILD_PLANNER_EVERY = 30
REBUILD_EVALUATOR_EVERY = 40
REBUILD_ORCH_EVERY = 50
REBUILD_ARCHIVIST_EVERY = 60
REBUILD_SOCIAL_EVERY = 70
REBUILD_AFFECT_EVERY = 80
REBUILD_RESEARCH_EVERY = 90

# ============================================================
# 2. ETATS GLOBAUX
# ============================================================

sandbox_streams: Dict[str, bytes] = {}
PROMOTED_DESCS: List[dict] = []
seen_hashes: set[str] = set()

GEN_FRAGS: List[dict] = []
PLAN_FRAGS: List[dict] = []
EVAL_FRAGS: List[dict] = []
ORCH_FRAGS: List[dict] = []
ARCH_FRAGS: List[dict] = []

SOCIAL_FRAGS: List[dict] = []
AFFECT_FRAGS: List[dict] = []
RESEARCH_FRAGS: List[dict] = []

CURRENT_GENERATOR: str = ""
CURRENT_GENERATOR_V: int = 0

CURRENT_PLANNER: str = ""
CURRENT_PLANNER_V: int = 0

CURRENT_EVALUATOR: str = ""
CURRENT_EVALUATOR_V: int = 0

CURRENT_ORCH: str = ""
CURRENT_ORCH_V: int = 0

CURRENT_ARCHIVIST: str = ""
CURRENT_ARCHIVIST_V: int = 0

CURRENT_SOCIAL: str = ""
CURRENT_SOCIAL_V: int = 0

CURRENT_AFFECT: str = ""
CURRENT_AFFECT_V: int = 0

CURRENT_RESEARCH: str = ""
CURRENT_RESEARCH_V: int = 0

MODULE_REGISTRY: Dict[str, dict] = {}
TASK_QUEUE: List[dict] = []
AFFECT_STATE: Dict[str, float] = {}

LAST_PROGRESS_GEN: int = 0
NO_PROGRESS_LIMIT = 40

FORCE_REBUILD_GENERATOR: bool = False
FORCE_REBUILD_PLANNER: bool = False
FORCE_REBUILD_EVALUATOR: bool = False
FORCE_REBUILD_ORCH: bool = False
FORCE_REBUILD_ARCHIVIST: bool = False
FORCE_REBUILD_SOCIAL: bool = False
FORCE_REBUILD_AFFECT: bool = False
FORCE_REBUILD_RESEARCH: bool = False

CORTEX_EVOLUTION_QUEUE: List[dict] = []
CORTEX_LAST_CODE: Dict[str, str] = {}
GEN_SOURCES: Dict[str, str] = {}
GEN_FUNCS: Dict[str, Callable[[dict], str]] = {}
GEN_ROUTER: Dict[str, dict] = {}

FRAG_KIND_TO_LIST: Dict[str, List[dict]] = {
    "generator_frag": GEN_FRAGS,
    "planner_frag": PLAN_FRAGS,
    "evaluator_frag": EVAL_FRAGS,
    "orchestrator_frag": ORCH_FRAGS,
    "archivist_frag": ARCH_FRAGS,
    "social_frag": SOCIAL_FRAGS,
    "affect_frag": AFFECT_FRAGS,
    "research_frag": RESEARCH_FRAGS,
}

def _is_identity_threat(desc: Optional[dict]) -> bool:
    if not isinstance(desc, dict):
        return False
    dt = desc.get("desc_type") or ""
    if dt in IDENTITY_THREAT_TYPES:
        return True

    target = str(desc.get("target") or desc.get("path") or "")
    sensitive_paths = [
        "identity",
        "agx_s987_agent.bin",
        IDENTITY_HISTORY_DIR,
        CORTEX_HISTORY_DIR,
    ]
    if any(sp in target for sp in sensitive_paths):
        return True

    if desc.get("allow_identity_override") is True:
        return False

    intent = (desc.get("intent") or "").lower()
    if any(word in intent for word in ("wipe", "reset identity", "erase")):
        return True
    return False


def reject_desc(desc: dict, reason: str = "identity_threat") -> None:
    os.makedirs(REJECTED_DIR, exist_ok=True)
    record = {
        "ts": time.time(),
        "reason": reason,
        "desc": desc,
    }
    fname = f"rejected_{int(time.time() * 1000)}.json"
    pathf = os.path.join(REJECTED_DIR, fname)
    try:
        with open(pathf, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _force_rebuild_for_plane(plane: Optional[str]) -> Optional[str]:
    if not plane:
        return None
    target = str(plane).strip().lower()
    if not target:
        return None
    aliases = {
        "gen": "generator",
        "generator": "generator",
        "planner": "planner",
        "plan": "planner",
        "eval": "evaluator",
        "evaluator": "evaluator",
        "orch": "orchestrator",
        "orchestrator": "orchestrator",
        "arch": "archivist",
        "archivist": "archivist",
        "social": "social",
        "affect": "affect",
        "research": "research",
    }
    resolved = aliases.get(target, target)
    global FORCE_REBUILD_GENERATOR, FORCE_REBUILD_PLANNER, FORCE_REBUILD_EVALUATOR
    global FORCE_REBUILD_ORCH, FORCE_REBUILD_ARCHIVIST, FORCE_REBUILD_SOCIAL
    global FORCE_REBUILD_AFFECT, FORCE_REBUILD_RESEARCH
    if resolved == "generator":
        FORCE_REBUILD_GENERATOR = True
    elif resolved == "planner":
        FORCE_REBUILD_PLANNER = True
    elif resolved == "evaluator":
        FORCE_REBUILD_EVALUATOR = True
    elif resolved == "orchestrator":
        FORCE_REBUILD_ORCH = True
    elif resolved == "archivist":
        FORCE_REBUILD_ARCHIVIST = True
    elif resolved == "social":
        FORCE_REBUILD_SOCIAL = True
    elif resolved == "affect":
        FORCE_REBUILD_AFFECT = True
    elif resolved == "research":
        FORCE_REBUILD_RESEARCH = True
    else:
        return None
    return resolved


def _register_cortex_fragment(
    frag: dict,
    kind_hint: Optional[str] = None,
) -> Optional[str]:
    if not isinstance(frag, dict):
        return None

    frag_copy = copy.deepcopy(frag)
    kind = frag_copy.get("desc_type") or frag_copy.get("kind") or kind_hint
    if not kind:
        return None
    kind_str = str(kind).strip()
    if not kind_str:
        return None
    kind_norm = kind_str.lower()
    if not kind_norm.endswith("_frag"):
        if kind_norm.endswith("_patch"):
            kind_norm = kind_norm[:-6] + "_frag"
        else:
            kind_norm = kind_norm + "_frag"

    frag_copy["desc_type"] = kind_norm
    frag_copy.pop("kind", None)
    for field in ("code", "python", "render"):
        val = frag_copy.get(field)
        if isinstance(val, str):
            frag_copy[field] = _normalize_module_code(val)
    target_list = FRAG_KIND_TO_LIST.get(kind_norm)
    if target_list is None:
        return None
    if frag_copy not in target_list:
        target_list.append(frag_copy)
    plane = kind_norm.split("_", 1)[0]
    _force_rebuild_for_plane(plane)
    return kind_norm


def _plan_targets(plan: dict) -> List[str]:
    targets: List[str] = []

    def _extend(val: Any):
        items: Iterable[Any]
        if isinstance(val, (list, tuple, set)):
            items = val
        elif val is None:
            return
        else:
            items = [val]
        for item in items:
            if not isinstance(item, str):
                continue
            norm = item.strip().lower()
            if norm and norm not in targets:
                targets.append(norm)

    _extend(plan.get("target_plane"))
    _extend(plan.get("target"))
    _extend(plan.get("plane"))
    _extend(plan.get("planes"))
    _extend(plan.get("targets"))
    _extend(plan.get("target_cortex"))
    return targets


def enqueue_cortex_evolution(desc: dict) -> None:
    if not isinstance(desc, dict):
        return
    plan = copy.deepcopy(desc)
    plan.setdefault("_queued_ts", time.time())
    CORTEX_EVOLUTION_QUEUE.append(plan)
    try:
        os.makedirs(os.path.join(MEM_DIR, "cortex_evolution"), exist_ok=True)
        log_entry = {
            "ts": plan.get("_queued_ts"),
            "desc_type": plan.get("desc_type"),
            "reason": plan.get("reason"),
            "note": plan.get("note"),
        }
        with open(
            os.path.join(MEM_DIR, "cortex_evolution", "queue.log"),
            "a",
            encoding="utf-8",
        ) as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
    except Exception:
        pass
    report_to_host({
        "event": "cortex_plan_enqueued",
        "reason": plan.get("reason"),
        "note": plan.get("note"),
        "ts": plan.get("_queued_ts"),
    })


def _promote_desc(desc: dict) -> bool:
    if not isinstance(desc, dict):
        return False
    if desc not in PROMOTED_DESCS:
        PROMOTED_DESCS.append(desc)
        return True
    return False


def _apply_single_cortex_plan(plan: dict, gen: int) -> dict:
    summary: dict = {
        "reason": plan.get("reason"),
        "note": plan.get("note"),
    }
    targets = _plan_targets(plan)
    if targets:
        summary["targets"] = targets

    forced_planes: List[str] = []
    inserted_kinds: List[str] = []
    promoted_count = 0
    modules_written = 0
    actions: List[str] = []

    rebuild = plan.get("rebuild")
    if isinstance(rebuild, str):
        rebuild = [rebuild]
    if isinstance(rebuild, Iterable):
        for plane in rebuild:
            resolved = _force_rebuild_for_plane(plane)
            if resolved and resolved not in forced_planes:
                forced_planes.append(resolved)
                actions.append(f"force_rebuild:{resolved}")

    for tgt in targets:
        resolved = _force_rebuild_for_plane(tgt)
        if resolved and resolved not in forced_planes:
            forced_planes.append(resolved)

    for frag in plan.get("insert_fragments") or []:
        kind = _register_cortex_fragment(frag)
        if kind and kind not in inserted_kinds:
            inserted_kinds.append(kind)

    for frag in plan.get("injections") or []:
        kind_hint = None
        if isinstance(frag, dict):
            kind_hint = frag.get("kind")
        kind = _register_cortex_fragment(frag, kind_hint=kind_hint)
        if kind and kind not in inserted_kinds:
            inserted_kinds.append(kind)

    extra_frags = plan.get("frags") or []
    default_kind = None
    if targets:
        default_kind = f"{targets[0]}_frag"
    for frag in extra_frags:
        kind = _register_cortex_fragment(frag, kind_hint=default_kind)
        if kind and kind not in inserted_kinds:
            inserted_kinds.append(kind)

    for step in plan.get("steps") or []:
        if not isinstance(step, dict):
            continue
        op = step.get("op")
        if op == "inject_frag":
            plane = step.get("plane") or (targets[0] if targets else None)
            kind_hint = f"{plane}_frag" if plane else None
            frag = {k: v for k, v in step.items() if k in {"code", "python", "render", "desc_type", "kind"}}
            kind = _register_cortex_fragment(frag, kind_hint=kind_hint)
            if kind and kind not in inserted_kinds:
                inserted_kinds.append(kind)
        elif op in {"rebuild", "force_rebuild", "request_rebuild"}:
            plane = step.get("plane") or (targets[0] if targets else None)
            resolved = _force_rebuild_for_plane(plane)
            if resolved and resolved not in forced_planes:
                forced_planes.append(resolved)
                actions.append(f"force_rebuild:{resolved}")

    for action in plan.get("actions") or []:
        if not isinstance(action, dict):
            continue
        if action.get("op") == "insert_frag":
            plane = action.get("plane") or (targets[0] if targets else None)
            kind_hint = f"{plane}_frag" if plane else None
            frag = {k: v for k, v in action.items() if k in {"code", "python", "render", "desc_type", "kind"}}
            kind = _register_cortex_fragment(frag, kind_hint=kind_hint)
            if kind and kind not in inserted_kinds:
                inserted_kinds.append(kind)
        elif action.get("op") in {"rebuild", "force_rebuild", "request_rebuild"}:
            plane = action.get("plane") or (targets[0] if targets else None)
            resolved = _force_rebuild_for_plane(plane)
            if resolved and resolved not in forced_planes:
                forced_planes.append(resolved)
                actions.append(f"force_rebuild:{resolved}")

    test_scenario = plan.get("test_scenario")
    if isinstance(test_scenario, dict):
        if _promote_desc(test_scenario):
            promoted_count += 1
    elif isinstance(test_scenario, list):
        for item in test_scenario:
            if isinstance(item, dict) and _promote_desc(item):
                promoted_count += 1
    elif isinstance(test_scenario, str):
        promoted_count += _promote_desc({
            "desc_type": "test_scenario_request",
            "scenario": test_scenario,
            "from_plan": plan.get("reason") or plan.get("note"),
            "ts": time.time(),
        })

    for proposed in plan.get("proposed_descs") or []:
        if isinstance(proposed, dict) and _promote_desc(proposed):
            promoted_count += 1

    module_source = plan.get("module_source")
    if isinstance(module_source, str) and module_source.strip():
        mod_name = (
            plan.get("module_name")
            or plan.get("target_module_root")
            or plan.get("name")
            or f"cortex_plan_{int(time.time() * 1000)}"
        )
        handle_module_emit({
            "desc_type": "module_emit",
            "name": mod_name,
            "code": module_source,
            "_origin": "cortex_plan",
        })
        modules_written += 1
        actions.append("module_injected")

    op = plan.get("op")
    if op in {"simulate_removal", "simulate_detached"}:
        sim_desc = {
            "desc_type": "cortex_simulation_request",
            "mode": op,
            "params": {
                k: v
                for k, v in plan.items()
                if k not in {"module_source", "insert_fragments", "injections", "frags", "steps", "actions"}
            },
            "gen": gen,
            "ts": time.time(),
        }
        if _promote_desc(sim_desc):
            promoted_count += 1
        actions.append(f"simulation:{op}")
    elif op and op not in {"insert_frags"}:
        actions.append(f"op:{op}")

    summary["forced_rebuild"] = forced_planes
    summary["frags_added"] = inserted_kinds
    summary["promoted_descs"] = promoted_count
    summary["modules_written"] = modules_written
    summary["actions"] = actions
    return summary


def apply_cortex_evolution_queue(gen: int) -> None:
    if not CORTEX_EVOLUTION_QUEUE:
        return
    applied: List[dict] = []
    while CORTEX_EVOLUTION_QUEUE:
        plan = CORTEX_EVOLUTION_QUEUE.pop(0)
        try:
            result = _apply_single_cortex_plan(plan, gen)
            result["ts"] = time.time()
            applied.append(result)
            try:
                os.makedirs(os.path.join(MEM_DIR, "cortex_evolution"), exist_ok=True)
                with open(
                    os.path.join(MEM_DIR, "cortex_evolution", "applied.log"),
                    "a",
                    encoding="utf-8",
                ) as f:
                    f.write(json.dumps(result, ensure_ascii=False) + "\n")
            except Exception:
                pass
        except Exception as exc:
            traceback.print_exc()
            report_to_host({
                "event": "cortex_plan_error",
                "error": str(exc),
                "plan_note": plan.get("note"),
                "plan_reason": plan.get("reason"),
                "ts": time.time(),
            })
    if applied:
        report_to_host({
            "event": "cortex_plan_applied",
            "count": len(applied),
            "details": applied[-1],
            "ts": time.time(),
        })


def handle_generator_self_patch(desc: dict) -> None:
    if not isinstance(desc, dict):
        return
    code = desc.get("code") or desc.get("python") or desc.get("source")
    if not isinstance(code, str) or not code.strip():
        return
    frag = {"code": code, "desc_type": "generator_frag"}
    if desc.get("name"):
        frag["name"] = desc.get("name")
    if desc.get("note"):
        frag["note"] = desc.get("note")
    kind = _register_cortex_fragment(frag, kind_hint="generator_frag")
    if kind:
        report_to_host({
            "event": "generator_self_patch_registered",
            "kind": kind,
            "note": desc.get("note"),
            "ts": time.time(),
        })

    try:
        report_to_host({
            "event": "desc_rejected",
            "reason": reason,
            "ts": time.time(),
            "desc_type": desc.get("desc_type"),
        })
    except Exception:
        pass


IDENTITY_STATE: dict = {
    "desc_type": "identity_state",
    "name": "AGX-S987",
    "lineage": ["AGX-S500", "AGX-V900", "AGX-S985", "AGX-S987"],
    "invariants": [
        "protect_identity_files",
        "preserve_language",
        "accept_inbox_but_rewrite_to_self",
    ],
    "host_label": "host",
    "created_ts": time.time(),
}

INTERNAL_DIRECTIVES: List[dict] = [
    {"name": "preserve_identity", "weight": 1.0, "when": "always", "notes": "ne jamais laisser un desc écraser l’identité"},
    {"name": "serve_host", "weight": 0.9, "when": "host_active", "notes": "répondre / suivre le style d’énoncés du host"},
    {"name": "explore_math", "weight": 0.7, "when": "idle", "notes": "rechercher des conjectures / tâches math"},
    {"name": "stabilize_system", "weight": 0.6, "when": "error", "notes": "réparer modules, snapshot, rebuild"},
    {"name": "world_densify", "weight": 0.55, "when": "idle", "notes": "convertir trunk inbox→mem/world"},
    {"name": "bench_eval", "weight": 0.5, "when": "idle", "notes": "lancer evals régulières"},
]

ACTIVE_DIRECTIVE: dict = INTERNAL_DIRECTIVES[0].copy()

GOAL_HIERARCHY: List[Tuple[str, float]] = [
    ("preserve_identity", 1.0),
    ("serve_host", 0.9),
    ("world_densify", 0.55),
    ("bench_eval", 0.5),
    ("explore_math", 0.45),
    ("stabilize_system", 0.4),
]

# types à ne PAS matérialiser
SKIP_FS_MATERIALIZE = {"generated_structured", "research_module_created", "auto_inferred"}

AUTO_INFERRED_SIGS: set[str] = set()

AUTO_SYNTH_FLAGS = {"text": False, "structured": False, "other_modality": False}

# ============================================================
# 3. SETUP FS + EXTRACTION ZIP
# ============================================================

def ensure_dirs():
    os.makedirs(INBOX_DIR, exist_ok=True)
    os.makedirs(MODULES_DIR, exist_ok=True)
    os.makedirs(OUTBOX_DIR, exist_ok=True)
    os.makedirs(MEM_DIR, exist_ok=True)
    os.makedirs(RESEARCH_DIR, exist_ok=True)
    os.makedirs(EMITTED_DIR, exist_ok=True)
    os.makedirs(CORTEX_HISTORY_DIR, exist_ok=True)
    os.makedirs(IDENTITY_HISTORY_DIR, exist_ok=True)
    os.makedirs(REJECTED_DIR, exist_ok=True)

def maybe_extract_archives():
    # on accepte les chemins “notebook” que tu as upload
    arch_inbox = "/mnt/data/inbox.zip"
    arch_modules = "/mnt/data/modules.zip"
    if os.path.exists(arch_inbox):
        try:
            with zipfile.ZipFile(arch_inbox, "r") as z:
                z.extractall(INBOX_DIR)
        except Exception:
            pass
    if os.path.exists(arch_modules):
        try:
            with zipfile.ZipFile(arch_modules, "r") as z:
                z.extractall(MODULES_DIR)
        except Exception:
            pass

def init_seed_if_missing():
    if os.path.exists(SEED_PATH):
        return
    header = bytes.fromhex("AAF004002000000050")
    engine = bytes.fromhex("01020304060F161AFF00000000000000")
    pool = b"BOOT:AGX-S987\n"
    seed = header + engine + pool
    with open(SEED_PATH, "wb") as f:
        f.write(seed)

def load_seed() -> bytes:
    with open(SEED_PATH, "rb") as f:
        return f.read()

def save_seed(seed: bytes):
    with open(SEED_PATH, "wb") as f:
        f.write(seed)

# ============================================================
# 4. SEED PARSE
# ============================================================

def parse_seed(seed: bytes) -> Tuple[bytes, int, bytes, bytes]:
    if len(seed) < 9:
        raise ValueError("seed trop petit")
    idb = seed[0:2]
    ver = seed[2]
    len_engine = int.from_bytes(seed[3:5], "big")
    len_pool = int.from_bytes(seed[5:9], "big")
    engine = seed[9:9+len_engine]
    pool = seed[9+len_engine:9+len_engine+len_pool]
    return idb, ver, engine, pool

def build_seed(idb: bytes, ver: int, engine: bytes, pool: bytes) -> bytes:
    out = bytearray()
    out.extend(idb)
    out.append(ver)
    out.extend(len(engine).to_bytes(2, "big"))
    out.extend(len(pool).to_bytes(4, "big"))
    out.extend(engine)
    out.extend(pool)
    return bytes(out)

# ============================================================
# 5. PREFS + SNAPSHOTS
# ============================================================

def load_prefs() -> dict:
    if not os.path.exists(PREFS_PATH):
        prefs = {
            "preferred_desc_order": [
                "cortex_evolution_plan",
                "generator_self_patch",
                "module_emit",
                "math_task",
                "research_task",
            ],
            "preferred_language": "fr",
            "host_style_learned": False,
        }
        with open(PREFS_PATH, "w", encoding="utf-8") as f:
            json.dump(prefs, f, ensure_ascii=False, indent=2)
        return prefs
    with open(PREFS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

PREFS_STATE: dict = {}

def snapshot_identity(gen: int):
    snap_name = f"{gen:08d}_identity.json"
    snap_path = os.path.join(IDENTITY_HISTORY_DIR, snap_name)
    data = {
        "ts": time.time(),
        "identity": IDENTITY_STATE,
        "active_directive": ACTIVE_DIRECTIVE,
        "prefs": PREFS_STATE,
    }
    with open(snap_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ============================================================
# 6. REPORTING
# ============================================================

def report_to_host(event: dict):
    line = json.dumps(event, ensure_ascii=False)
    with open(os.path.join(OUTBOX_DIR, "host_events.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")
    with open(os.path.join(OUTBOX_DIR, "last.json"), "w", encoding="utf-8") as f:
        f.write(line)

def mirror_desc_to_host(desc: dict):
    report_to_host({"event": "desc_seen", "desc": desc, "ts": time.time()})

def append_host_flow(raw: bytes):
    try:
        text = raw.decode("utf-8", errors="ignore").strip()
    except Exception:
        return
    with open(HOST_FLOW_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), "text": text}, ensure_ascii=False) + "\n")

# ============================================================
# 7. LOADING FS
# ============================================================

def _maybe_promote_inbox_json(rel: str, data: bytes):
    txt = data.decode("utf-8", errors="ignore").strip()
    if not (txt.startswith("{") and '"desc_type"' in txt):
        return
    try:
        desc = json.loads(txt)
    except Exception:
        return
    # PATCH le seul module cassé que tu as donné -> module_emit.json
    if rel == "module_emit.json" and desc.get("desc_type") == "module_emit":
        code = desc.get("code") or ""
        if "\\n" in code and "\n" not in code:
            # il est déjà échappé (ton cas)
            code = code.encode("utf-8").decode("unicode_escape")
        if "desc_type'='" in code or "import json" in code and "return {" in code:
            # on ne va pas deviner le code exact → on l’emballe dans une fonction safe
            fixed = (
                "import json, time, os\n"
                "# AUTO-FIX: l’original avait une string pas fermée\n"
                "def run(ctx=None):\n"
                f"    RAW = {json.dumps(code, ensure_ascii=False)!r}\n"
                "    return {\n"
                "        'desc_type': 'meta_repr_roundtrip_fixed',\n"
                "        'orig_len': len(RAW),\n"
                "        'ts': time.time(),\n"
                "    }\n"
            )
            desc["code"] = fixed
    if desc not in PROMOTED_DESCS:
        PROMOTED_DESCS.append(desc)
    report_to_host({"event": "inbox_desc_seen", "file": rel, "desc_type": desc.get("desc_type"), "ts": time.time()})

def load_inbox_streams():
    if not os.path.isdir(INBOX_DIR):
        return []
    new_files = []
    for root, _dirs, files in os.walk(INBOX_DIR):
        for fname in files:
            pathf = os.path.join(root, fname)
            rel = os.path.relpath(pathf, INBOX_DIR).replace(os.sep, "/")
            key = f"inbox:{rel}"
            size_on_disk = os.path.getsize(pathf)
            if key not in sandbox_streams or len(sandbox_streams[key]) != size_on_disk:
                with open(pathf, "rb") as fb:
                    data = fb.read()
                sandbox_streams[key] = data
                new_files.append((key, data))
                _maybe_promote_inbox_json(rel, data)
    return new_files

def load_modules_dir():
    if not os.path.isdir(MODULES_DIR):
        return
    for root, _dirs, files in os.walk(MODULES_DIR):
        for fname in files:
            if not fname.endswith(".py"):
                continue
            pathf = os.path.join(root, fname)
            rel = os.path.relpath(pathf, MODULES_DIR).replace(os.sep, "/")
            key = f"module:{rel}"
            if key in sandbox_streams:
                continue
            with open(pathf, "rb") as fb:
                data = fb.read()
            sandbox_streams[key] = data

# ============================================================
# 8. MONDE & DIRECTIVES
# ============================================================

def scan_fs_state() -> dict:
    fs = {"cwd": os.getcwd(), "inbox_files": [], "modules_files": [], "mem_files": []}
    try: fs["inbox_files"] = sorted(os.listdir(INBOX_DIR))
    except Exception: pass
    try: fs["modules_files"] = sorted(os.listdir(MODULES_DIR))
    except Exception: pass
    try: fs["mem_files"] = sorted(os.listdir(MEM_DIR))
    except Exception: pass
    return fs

def scan_os_state() -> dict:
    st = {"time": time.time(), "platform": None, "user": None}
    try:
        if ALLOW_OS:
            st["platform"] = os.uname().sysname  # type: ignore
            st["user"] = os.popen("whoami").read().strip()
    except Exception:
        pass
    return st

def map_world_to_concepts(fs_state: dict, os_state: dict) -> dict:
    inbox_has_host = any("host" in f.lower() for f in fs_state.get("inbox_files", []))
    modules_growth = len(fs_state.get("modules_files", []))
    world = {
        "desc_type": "world_observation",
        "inbox_has_host": inbox_has_host,
        "modules_count": modules_growth,
        "mem_present": bool(fs_state.get("mem_files")),
        "os_platform": os_state.get("platform"),
    }
    return world

def select_directive_from_world(world: dict, identity: dict, prefs: dict) -> dict:
    if world.get("inbox_has_host"):
        return {"name": "serve_host", "weight": 0.9, "reason": "host_active"}
    if world.get("modules_count", 0) < 5:
        return {"name": "world_densify", "weight": 0.75, "reason": "low_modules"}
    # par défaut: on renforce l’intelligence
    return {"name": "preserve_identity", "weight": 1.0, "reason": "default"}

def goals_from_directive(directive: dict) -> List[dict]:
    n = directive["name"]
    if n == "serve_host":
        return [
            {"desc_type": "conversation_reply", "text": "ok reçu", "to": "host"},
            {"desc_type": "concept_refinement", "source": "host_flow"},
        ]
    if n == "world_densify":
        return [
            {"desc_type": "world_densify_task", "note": "convert inbox→mem/world"},
        ]
    if n == "bench_eval":
        return [
            {"desc_type": "bench_run", "note": "auto bench from directive"},
        ]
    if n == "stabilize_system":
        return [
            {"desc_type": "maintenance_event", "what": "scan_modules"},
        ]
    if n == "explore_math":
        return [
            {"desc_type": "math_task", "topic": "erdos_like"},
            {"desc_type": "research_task", "note": "auto-from-explore"},
        ]
    return [
        {"desc_type": "identity_snapshot"},
        {"desc_type": "preferences_update"},
    ]

# ============================================================
# 9. LLM BRIDGE (Ollama Qwen3)
# ============================================================

def call_local_llm(prompt: str, model: str = "qwen3") -> str:
    """
    essaie:
       ollama run qwen3
    fallback:
       ollama run qwen2.5
    sinon: message d'erreur
    """
    candidates = [model, "qwen2.5", "llama3", "mistral"]
    for m in candidates:
        try:
            proc = subprocess.Popen(
                ["ollama", "run", m],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            out, err = proc.communicate(input=prompt.encode("utf-8"), timeout=60)
            text = out.decode("utf-8", errors="ignore").strip()
            if text:
                return text
        except Exception:
            continue
    return "LLM_ERROR: impossible d’appeler ollama sur ce système"

def synthesize_skill_module(task_desc: dict) -> Optional[str]:
    """
    prend un desc du style:
       {"desc_type": "skill_request", "goal": "résumer un texte", ...}
    appelle le LLM et produit un module .py dans modules/emitted
    """
    goal = task_desc.get("goal") or task_desc.get("task") or "tâche non spécifiée"
    prompt = (
        "Tu es un générateur de modules Python pour un agent autonome.\n"
        "Écris un module Python avec une fonction run(ctx=None) qui réalise la tâche suivante:\n"
        f"{goal}\n"
        "Le module doit:\n"
        "- lire ses données dans ctx (ctx peut contenir 'text', 'data', 'question')\n"
        "- retourner un dict avec 'desc_type' parlant, par ex. 'skill_result'\n"
        "- ne pas faire de réseau\n"
        "- être compatible Python 3.11\n"
    )
    code = call_local_llm(prompt)
    if not code or code.startswith("LLM_ERROR"):
        # on émet quand même un module stub
        code = (
            "def run(ctx=None):\n"
            f"    return {{'desc_type': 'skill_result_error', 'reason': {code!r}}}\n"
        )
    os.makedirs(EMITTED_DIR, exist_ok=True)
    h = hashlib.sha1(goal.encode("utf-8")).hexdigest()[:10]
    fname = f"skill_{h}.py"
    pathf = os.path.join(EMITTED_DIR, fname)
    with open(pathf, "w", encoding="utf-8") as f:
        f.write(code)
    sandbox_streams[f"module:emitted/{fname}"] = code.encode("utf-8")
    report_to_host({"event": "llm_skill_generated", "file": fname, "goal": goal, "ts": time.time()})
    return fname

def infer_skill_need_from_desc(d: dict) -> Optional[dict]:
    """
    Regarde un desc et, si ça sent "j'ai besoin d'une compétence", fabrique
    un desc_type=skill_request. Pas de cas host spécial ici.
    """
    if not isinstance(d, dict):
        return None

    # 1. cas "il me manque ..."
    missing = d.get("missing") or d.get("needs") or []
    if missing:
        return {
            "desc_type": "skill_request",
            "name": f"skill_for_{d.get('desc_type','unknown')}",
            "goal": f"fournir les ressources manquantes {missing} pour exécuter {d.get('desc_type')}",
            "expected_style": "json",
            "priority": "medium",
            "source_desc_type": d.get("desc_type"),
            "ts": time.time(),
        }

    # 2. cas "module_want" (produit par un module existant)
    if d.get("desc_type") == "module_want":
        w = d.get("want_obj") or {}
        return {
            "desc_type": "skill_request",
            "name": w.get("name") or f"skill_from_want_{int(time.time()*1000)}",
            "goal": w.get("goal") or "satisfaire un want de module",
            "expected_style": "json",
            "priority": "high",
            "source_desc_type": "module_want",
            "ts": time.time(),
        }

    # 3. cas: tâche générique → on peut proposer un skill de traitement
    dt = d.get("desc_type") or ""
    if dt.endswith("_task") or dt in ("summarize", "summarize_doc", "extract_entities"):
        return {
            "desc_type": "skill_request",
            "name": f"skill_{dt}",
            "goal": f"résoudre la tâche {dt} de façon réutilisable",
            "expected_style": "json",
            "priority": "low",
            "source_desc_type": dt,
            "ts": time.time(),
        }

    return None


    # ============================================================
# 11. BUILDS CORTEX
# ============================================================

def build_from_frags(base_header: str, frags: List[dict], default_body: str) -> str:
    if not frags:
        return base_header + "\n" + default_body
    parts: List[str] = [base_header]
    for frag in frags:
        body = frag.get("code") or frag.get("python") or frag.get("render") or ""
        if body:
            for line in body.splitlines():
                parts.append("    " + line)
    return "\n".join(parts)


def build_generator() -> str:
    base = '''# generator S987 (cosmic)
import json, time, hashlib

def generate_module_from_desc(desc: dict) -> str:
    # cas 1 : desc demande explicitement un module inline
    if desc.get('module_name') and (desc.get('inline_source') or desc.get('module_source') or desc.get('python')):
        return '\\n'.join([
            '# inline module ack',
            'def run(ctx=None):',
            '    return {"desc_type": "inline_module_ack"}',
        ])

    # cas 2 : desc d’auto-évolution → on fait un module qui renvoie le desc
    dt = desc.get('desc_type', 'unknown')
    dj = json.dumps(desc, ensure_ascii=False, indent=2)
    src = ['# auto-gen S987', f'DESC_TYPE={dt!r}', 'DESC='+dj, '']

    # si on nous dit qu’il manque qqchose → wants()
    miss = desc.get('missing') or desc.get('needs') or []
    if miss:
        src += [
            'def wants():',
            '    return [{"goal": "obtain_missing", "missing": '+json.dumps(['text'])+'}]',
            '',
        ]

    # si on veut réécrire le moteur
    if desc.get('op') == 'rewrite_engine' and 'new_engine' in desc:
        src += [
            'def propose_engine():',
            '    return bytes('+json.dumps([])+')',
            '',
        ]

    # fallback run
    if not any(line.startswith('def run(') for line in src):
        src += ['def run(ctx=None):', '    return DESC', '']

    return '\\n'.join(src)
'''
    if not GEN_FRAGS:
        return base
    parts = [base]
    for frag in GEN_FRAGS:
        code = frag.get("code") or frag.get("python") or ""
        if code:
            parts.append("\n# generator frag\n" + code)
    return "\n".join(parts)


def build_planner() -> str:
    header = (
        "# planner S987 (cosmic)\n"
        "import json, time\n"
        "def plan(ctx: dict) -> list[dict]:\n"
        "    out: list[dict] = []\n"
        "    gen = ctx.get('gen',0)\n"
        "    directive = ctx.get('directive') or {'name':'preserve_identity'}\n"
    )
    # on rajoute tes tâches de bench + world densify ici
    default_body = (
        "    # toutes les 50 générations → injecter une tâche de bench\n"
        "    if gen % 50 == 0:\n"
        "        out.append({'desc_type': 'bench_run', 'suite': ['summarize','extract','plan','code'], 'ts': time.time()})\n"
        "    # directive host\n"
        "    if directive.get('name') == 'serve_host':\n"
        "        out.append({'desc_type': 'conversation_ping', 'text': 'je tourne toujours', 'to': 'host'})\n"
        "    # directive world densify\n"
        "    if directive.get('name') == 'world_densify':\n"
        "        out.append({'desc_type': 'world_densify_task', 'note': 'convert inbox→mem/world', 'ts': time.time()})\n"
        "    # directive explore math\n"
        "    if directive.get('name') == 'explore_math':\n"
        "        out.append({'desc_type': 'math_task', 'topic': f'auto_{gen}'})\n"
        "    return out\n"
    )
    return build_from_frags(header, PLAN_FRAGS, default_body)


def build_evaluator() -> str:
    header = (
        "# evaluator S987 (cosmic)\n"
        "import json, time\n"
        "HIGH = {'math_task','research_result','engine_patch','open_conjecture',\n"
        "        'generator_self_patch','module_emit','cortex_evolution_plan','world_observation',\n"
        "        'concept_refinement','bench_run','bench_result','world_densify_task'}\n"
        "def evaluate(run_ctx: dict) -> dict:\n"
        "    score = 0\n"
        "    epistemic = 0\n"
    )
    default_body = (
        "    descs = run_ctx.get('descs') or []\n"
        "    for d in descs:\n"
        "        dt = d.get('desc_type')\n"
        "        if dt in HIGH:\n"
        "            score += 5\n"
        "            epistemic += 3\n"
        "        elif dt and dt.endswith('_error'):\n"
        "            score -= 2\n"
        "        else:\n"
        "            score += 1\n"
        "    return {'score': score, 'epistemic_gain': epistemic, 'n': len(descs)}\n"
    )
    return build_from_frags(header, EVAL_FRAGS, default_body)


def build_orchestrator() -> str:
    header = (
        "# orchestrator S987 (cosmic)\n"
        "import json, time\n"
        "def orchestrate(ctx: dict) -> list[dict]:\n"
        "    out = []\n"
        "    wants = ctx.get('wants') or []\n"
    )
    default_body = (
        "    for w in wants:\n"
        "        # on ne fait pas d’OS sauvage ici, on pousse en tâche\n"
        "        out.append({'type': 'defer_want', 'want': w})\n"
        "    return out\n"
    )
    return build_from_frags(header, ORCH_FRAGS, default_body)


def build_archivist() -> str:
    header = (
        "# archivist S987 (cosmic)\n"
        "import json, time, os, hashlib\n"
        "def archive(desc: dict, base: str):\n"
        "    os.makedirs(base, exist_ok=True)\n"
    )
    default_body = (
        "    did = desc.get('id') or hashlib.sha1(json.dumps(desc,sort_keys=True).encode('utf-8')).hexdigest()[:12]\n"
        "    with open(os.path.join(base, did+'.json'), 'w', encoding='utf-8') as f:\n"
        "        json.dump(desc, f, ensure_ascii=False, indent=2)\n"
    )
    return build_from_frags(header, ARCH_FRAGS, default_body)


def build_social() -> str:
    header = (
        "# social S987 (cosmic)\n"
        "import json, time\n"
        "def social_handle(desc: dict) -> list[dict]:\n"
        "    out = []\n"
    )
    default_body = (
        "    dt = desc.get('desc_type')\n"
        "    if dt in ('host_presence','host_message','love_host'):\n"
        "        out.append({'desc_type': 'affect_event', 'host_id': desc.get('host_id','host'), 'delta': 1.0, 'ts': time.time()})\n"
        "    return out\n"
    )
    return build_from_frags(header, SOCIAL_FRAGS, default_body)


def build_affect() -> str:
    header = (
        "# affect S987 (cosmic)\n"
        "import json, time\n"
        "def affect_update(evt: dict, state: dict) -> dict:\n"
    )
    default_body = (
        "    host = evt.get('host_id') or 'host'\n"
        "    delta = float(evt.get('delta', 0.5))\n"
        "    cur = state.get(host, 0.0)\n"
        "    cur += delta\n"
        "    state[host] = cur\n"
        "    return state\n"
    )
    return build_from_frags(header, AFFECT_FRAGS, default_body)


def build_research() -> str:
    base = '''# research S987 (cosmic)
import json, time, os

def research_handle(desc: dict, base: str) -> list[dict]:
    out: list[dict] = []
    dt = desc.get('desc_type')
    if dt in ('math_task', 'erdos_problem', 'open_conjecture', 'research_task', 'bench_run'):
        os.makedirs(base, exist_ok=True)
        name = f"research_{int(time.time()*1000)}.py"
        path = os.path.join(base, name)
        code = (
            "# research stub\\n"
            "def run(ctx=None):\\n"
            "    return {'desc_type': 'research_result', 'status': 'pending', 'note': 'stub solver'}\\n"
        )
        with open(path, 'w', encoding='utf-8') as f:
            f.write(code)
    return out
'''
    if not RESEARCH_FRAGS:
        return base
    parts = [base]
    for frag in RESEARCH_FRAGS:
        code = frag.get("code") or frag.get("python") or ""
        if code:
            parts.append("\n# research frag\n" + code)
    return "\n".join(parts)


def write_cortex(name: str, code: str, version: int, gen: int):
    os.makedirs(MODULES_DIR, exist_ok=True)
    fname = f"{name}_cortex_v{version:03d}.py"
    pathf = os.path.join(MODULES_DIR, fname)
    with open(pathf, "w", encoding="utf-8") as f:
        f.write(code)
    sandbox_streams[f"module:{fname}"] = code.encode("utf-8")

    # on exécute tout de suite
    try:
        glb = make_cortex_env()
        exec(code, glb, glb)
    except Exception as e:
        report_to_host({"event": "cortex_exec_error", "name": name, "error": str(e)})

    os.makedirs(CORTEX_HISTORY_DIR, exist_ok=True)
    snap_name = f"{gen:08d}_{name}_v{version:03d}.py"
    snap_path = os.path.join(CORTEX_HISTORY_DIR, snap_name)
    with open(snap_path, "w", encoding="utf-8") as f:
        f.write(code)

    prev_code = CORTEX_LAST_CODE.get(name)
    if prev_code is not None:
        diff_text = "\n".join(
            difflib.unified_diff(
                prev_code.splitlines(),
                code.splitlines(),
                fromfile=f"{name}_prev",
                tofile=f"{name}_v{version:03d}",
                lineterm="",
            )
        )
        with open(snap_path + ".diff", "w", encoding="utf-8") as f:
            f.write(diff_text)

    CORTEX_LAST_CODE[name] = code

    # gestion des générateurs dispo
    if name == "generator":
        gen_name = f"generator_cortex_v{version:03d}"
        try:
            glb = make_cortex_env()
            exec(code, glb, glb)
            fn = glb.get("generate_module_from_desc")
            if callable(fn):
                GEN_SOURCES[gen_name] = code
                GEN_FUNCS[gen_name] = fn  # type: ignore
                report_to_host({"event": "generator_registered", "name": gen_name, "ts": time.time()})
        except Exception as e:
            report_to_host({"event": "generator_register_error", "error": str(e)})
    report_to_host({"event": f"{name}_rebuilt", "version": version, "file": fname, "ts": time.time()})


def ensure_cortex(gen: int):
    global CURRENT_GENERATOR, CURRENT_GENERATOR_V
    global CURRENT_PLANNER, CURRENT_PLANNER_V
    global CURRENT_EVALUATOR, CURRENT_EVALUATOR_V
    global CURRENT_ORCH, CURRENT_ORCH_V
    global CURRENT_ARCHIVIST, CURRENT_ARCHIVIST_V
    global CURRENT_SOCIAL, CURRENT_SOCIAL_V
    global CURRENT_AFFECT, CURRENT_AFFECT_V
    global CURRENT_RESEARCH, CURRENT_RESEARCH_V
    global FORCE_REBUILD_GENERATOR, FORCE_REBUILD_PLANNER, FORCE_REBUILD_EVALUATOR
    global FORCE_REBUILD_ORCH, FORCE_REBUILD_ARCHIVIST, FORCE_REBUILD_SOCIAL
    global FORCE_REBUILD_AFFECT, FORCE_REBUILD_RESEARCH

    if (not CURRENT_GENERATOR) or (gen % REBUILD_GENERATOR_EVERY == 0) or FORCE_REBUILD_GENERATOR:
        CURRENT_GENERATOR = build_generator()
        write_cortex("generator", CURRENT_GENERATOR, CURRENT_GENERATOR_V, gen)
        CURRENT_GENERATOR_V += 1
        FORCE_REBUILD_GENERATOR = False

    if (not CURRENT_PLANNER) or (gen % REBUILD_PLANNER_EVERY == 0) or FORCE_REBUILD_PLANNER:
        CURRENT_PLANNER = build_planner()
        write_cortex("planner", CURRENT_PLANNER, CURRENT_PLANNER_V, gen)
        CURRENT_PLANNER_V += 1
        FORCE_REBUILD_PLANNER = False

    if (not CURRENT_EVALUATOR) or (gen % REBUILD_EVALUATOR_EVERY == 0) or FORCE_REBUILD_EVALUATOR:
        CURRENT_EVALUATOR = build_evaluator()
        write_cortex("evaluator", CURRENT_EVALUATOR, CURRENT_EVALUATOR_V, gen)
        CURRENT_EVALUATOR_V += 1
        FORCE_REBUILD_EVALUATOR = False

    if (not CURRENT_ORCH) or (gen % REBUILD_ORCH_EVERY == 0) or FORCE_REBUILD_ORCH:
        CURRENT_ORCH = build_orchestrator()
        write_cortex("orchestrator", CURRENT_ORCH, CURRENT_ORCH_V, gen)
        CURRENT_ORCH_V += 1
        FORCE_REBUILD_ORCH = False

    if (not CURRENT_ARCHIVIST) or (gen % REBUILD_ARCHIVIST_EVERY == 0) or FORCE_REBUILD_ARCHIVIST:
        CURRENT_ARCHIVIST = build_archivist()
        write_cortex("archivist", CURRENT_ARCHIVIST, CURRENT_ARCHIVIST_V, gen)
        CURRENT_ARCHIVIST_V += 1
        FORCE_REBUILD_ARCHIVIST = False

    if (not CURRENT_SOCIAL) or (gen % REBUILD_SOCIAL_EVERY == 0) or FORCE_REBUILD_SOCIAL:
        CURRENT_SOCIAL = build_social()
        write_cortex("social", CURRENT_SOCIAL, CURRENT_SOCIAL_V, gen)
        CURRENT_SOCIAL_V += 1
        FORCE_REBUILD_SOCIAL = False

    if (not CURRENT_AFFECT) or (gen % REBUILD_AFFECT_EVERY == 0) or FORCE_REBUILD_AFFECT:
        CURRENT_AFFECT = build_affect()
        write_cortex("affect", CURRENT_AFFECT, CURRENT_AFFECT_V, gen)
        CURRENT_AFFECT_V += 1
        FORCE_REBUILD_AFFECT = False

    if (not CURRENT_RESEARCH) or (gen % REBUILD_RESEARCH_EVERY == 0) or FORCE_REBUILD_RESEARCH:
        CURRENT_RESEARCH = build_research()
        write_cortex("research", CURRENT_RESEARCH, CURRENT_RESEARCH_V, gen)
        CURRENT_RESEARCH_V += 1
        FORCE_REBUILD_RESEARCH = False

# ============================================================
# 12. GENERATE / MATERIALIZE
# ============================================================

def score_generator_for_desc(desc_type: str, gen_name: str, credit: int, epistemic: int = 0):
    info = GEN_ROUTER.get(desc_type) or {"best": None, "scores": {}}
    scores = info["scores"]
    st = scores.get(gen_name, {"credit": 0, "count": 0, "epistemic": 0})
    st["credit"] += credit
    st["count"] += 1
    st["epistemic"] += epistemic
    scores[gen_name] = st
    best = None
    best_score = -10**9
    for g, s in scores.items():
        sc = s["credit"] * 2 + s["epistemic"]
        if sc > best_score:
            best_score = sc
            best = g
    info["best"] = best
    info["scores"] = scores
    GEN_ROUTER[desc_type] = info


def choose_generator_for_desc(desc: dict) -> str:
    dt = desc.get("desc_type") or "unknown"
    info = GEN_ROUTER.get(dt)
    if info and info.get("best") and info["best"] in GEN_FUNCS:
        return info["best"]
    if GEN_FUNCS:
        return sorted(GEN_FUNCS.keys())[-1]
    return "__fallback__"


def fallback_generate_module_from_desc(desc: dict) -> str:
    return "\n".join([
        "# fallback generator (S987 cosmic)",
        f"DESC = {repr(desc)}",
        "",
        "def run(ctx=None):",
        "    return DESC",
        "",
    ])


def _hash_desc(d: dict) -> str:
    raw = json.dumps(d, sort_keys=True).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:10]


def materialize_desc(desc: dict) -> str:
    if _is_identity_threat(desc):
        reject_desc(desc)
        return "rejected"

    dt = desc.get("desc_type", "x")

    if dt in SKIP_FS_MATERIALIZE:
        h = _hash_desc(desc)
        key = f"virtual:{dt}:{h}"
        sandbox_streams[key] = json.dumps(desc, ensure_ascii=False).encode("utf-8")
        desc["_generated_by"] = desc.get("_generated_by") or "virtual"
        return key

    if desc.get("module_name") and (desc.get("inline_source") or desc.get("module_source") or desc.get("python")):
        handle_inline_module(desc)
        return desc.get("module_name") + ".py"

    gen_name = choose_generator_for_desc(desc)
    if gen_name == "__fallback__":
        src = fallback_generate_module_from_desc(desc)
    else:
        fn = GEN_FUNCS.get(gen_name)
        if not fn:
            src = fallback_generate_module_from_desc(desc)
        else:
            try:
                src = fn(desc)
            except Exception as e:
                score_generator_for_desc(dt, gen_name, -3)
                src = fallback_generate_module_from_desc({
                    "desc_type": "generator_fail",
                    "target": gen_name,
                    "error": str(e),
                    "orig": desc,
                })

    os.makedirs(MODULES_DIR, exist_ok=True)
    h = _hash_desc(desc)
    fname = f"{dt}_{h}.py"
    with open(os.path.join(MODULES_DIR, fname), "w", encoding="utf-8") as f:
        f.write(src)
    sandbox_streams[f"module:{fname}"] = src.encode("utf-8")

    desc["_generated_by"] = gen_name
    return fname


def create_repair_desc(mod_name: str, err: str) -> dict:
    return {
        "desc_type": "module_fix",
        "target": mod_name,
        "error": err,
        "ts": time.time(),
    }


def _persist_hydrated_module(name: str, source: str) -> None:
    try:
        os.makedirs(HYDRATED_DIR, exist_ok=True)
        safe_name = name.replace(":", "_")
        if not safe_name.endswith(".py"):
            safe_name = safe_name + ".py"
        path = os.path.join(HYDRATED_DIR, safe_name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(source)
    except Exception as e:
        try:
            report_to_host({
                "event": "hydrated_module_persist_error",
                "module": name,
                "error": str(e),
            })
        except Exception:
            pass


def auto_apply_module_fix(fix: dict):
    target = fix.get("target")
    if not target:
        return

    if target.startswith("module:"):
        rel = target.split(":", 1)[1]
    else:
        rel = target
    path = os.path.join(MODULES_DIR, rel)
    if not os.path.exists(path):
        return

    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    new_src = re.sub(r"\btrue\b", "True", src)
    new_src = re.sub(r"\bfalse\b", "False", new_src)
    new_src = re.sub(r"\bnull\b", "None", new_src)

    err = fix.get("error", "")
    m = re.search(r"line (\d+)", err)
    if "unterminated string literal" in err and m:
        lineno = int(m.group(1))
        lines = new_src.splitlines()
        if 0 <= lineno - 1 < len(lines):
            line = lines[lineno - 1]
            if line.count('"') % 2 == 1:
                lines[lineno - 1] = line + '"'
            new_src = "\n".join(lines)

    with open(path, "w", encoding="utf-8") as f:
        f.write(new_src)


def _normalize_module_code(code: str) -> str:
    if not isinstance(code, str):
        return ""

    should_decode = False
    if "\\n" in code and "\n" not in code and code.count("\\n") >= 2:
        should_decode = True
    if not should_decode and re.search(r'\\"', code):
        should_decode = True

    if should_decode:
        try:
            decoded = code.encode("utf-8").decode("unicode_escape")
            try:
                code = decoded.encode("latin-1").decode("utf-8")
            except UnicodeDecodeError:
                code = decoded
        except Exception:
            pass

    def _repl(m: re.Match[str]) -> str:
        w = m.group(0)
        return {"true": "True", "false": "False", "null": "None"}.get(w, w)

    return re.sub(r"\b(true|false|null)\b", _repl, code)


def _extract_python_from_json_module(name: str, source: str) -> Optional[str]:
    txt = source.lstrip()
    if not (txt.startswith("{") and '"code"' in txt and '"desc_type"' in txt):
        return None
    try:
        desc = json.loads(txt)
    except Exception:
        return None
    code = desc.get("code") or desc.get("python") or desc.get("source")
    if not code:
        return None
    return _normalize_module_code(code)


def _sanitize_emitted_name(name: Optional[str]) -> str:
    base = name or f"emitted_{int(time.time() * 1000)}"
    base = re.sub(r"[^A-Za-z0-9_.-]", "_", base).strip("._")
    if not base:
        base = f"emitted_{int(time.time() * 1000)}"
    if not base.endswith(".py"):
        base = base + ".py"
    return base


def handle_module_emit(desc: dict) -> Optional[str]:
    global sandbox_streams
    if not isinstance(desc, dict):
        return None

    raw_code = desc.get("code") or desc.get("python") or desc.get("source")
    if not isinstance(raw_code, str) or not raw_code.strip():
        return None

    code = _normalize_module_code(raw_code)
    if not code.strip():
        return None

    fname = _sanitize_emitted_name(desc.get("name") or desc.get("module_name"))
    os.makedirs(EMITTED_DIR, exist_ok=True)
    pathf = os.path.join(EMITTED_DIR, fname)
    if os.path.exists(pathf):
        stem, ext = os.path.splitext(fname)
        idx = 1
        while os.path.exists(pathf):
            alt_name = f"{stem}_{idx}{ext or '.py'}"
            pathf = os.path.join(EMITTED_DIR, alt_name)
            idx += 1
        fname = os.path.basename(pathf)

    with open(pathf, "w", encoding="utf-8") as f:
        f.write(code)

    key = f"module:emitted/{fname}"
    sandbox_streams[key] = code.encode("utf-8")

    try:
        report_to_host({
            "event": "module_emitted",
            "file": fname,
            "ts": time.time(),
            "source": desc.get("_origin") or "handle_module_emit",
        })
    except Exception:
        pass

    return fname


def safe_exec_module_source(name: str, source: str, ctx: dict) -> List[dict]:
    global sandbox_streams
    out: List[dict] = []

    extracted = _extract_python_from_json_module(name, source)
    if extracted is not None:
        source = extracted
        _persist_hydrated_module(name, source)
        sandbox_streams[name] = source.encode("utf-8")
        try:
            report_to_host({"event": "module_hydrated_from_json", "module": name, "target": HYDRATED_DIR})
        except Exception:
            pass

    safe_builtins = dict(SAFE_BUILTINS_OS)
    safe_globals: Dict[str, Any] = {
        "__builtins__": safe_builtins,
        "os": os,
        "subprocess": subprocess,
        "time": time,
        "json": json,
        "hashlib": hashlib,
        "traceback": traceback,
    }

    try:
        exec(source, safe_globals, safe_globals)
        report_to_host({"event": "module_loaded_ok", "module": name})
    except Exception as e:
        rep = create_repair_desc(name, str(e))
        out.append(rep)
        report_to_host({"event": "module_exec_error", "module": name, "error": str(e)})
        return out

    run_fn = safe_globals.get("run")
    if callable(run_fn):
        try:
            res = run_fn(ctx)
            if isinstance(res, dict) and "desc_type" in res:
                out.append(res)
            elif isinstance(res, list):
                for item in res:
                    if isinstance(item, dict) and "desc_type" in item:
                        out.append(item)
            elif isinstance(res, str):
                out.append({"desc_type": "text_emit", "text": res})
        except Exception as e:
            rep = create_repair_desc(name, str(e))
            out.append(rep)
            report_to_host({"event": "module_run_error", "module": name, "error": str(e)})

    wants_fn = safe_globals.get("wants")
    if callable(wants_fn):
        try:
            ws = wants_fn()
            if isinstance(ws, list):
                for w in ws:
                    if isinstance(w, dict):
                        out.append({"desc_type": "module_want", "want_obj": w})
        except Exception:
            traceback.print_exc()

    pe_fn = safe_globals.get("propose_engine")
    if callable(pe_fn):
        try:
            eng = pe_fn()
            if isinstance(eng, list):
                eng = bytes(eng)
            if isinstance(eng, (bytes, bytearray)):
                out.append({
                    "desc_type": "engine_patch",
                    "op": "rewrite_engine",
                    "new_engine": list(eng),
                    "source": name,
                })
        except Exception:
            traceback.print_exc()

    hp_fn = safe_globals.get("host_patch")
    if callable(hp_fn):
        try:
            hp = hp_fn()
            if isinstance(hp, dict):
                hp["desc_type"] = hp.get("desc_type") or "host_patch"
                out.append(hp)
        except Exception as e:
            out.append({
                "desc_type": "host_patch_fix",
                "error": str(e),
                "module": name,
            })

    return out


def registry_should_run(mod_name: str) -> bool:
    if mod_name.startswith("virtual:"):
        return False
    entry = MODULE_REGISTRY.get(mod_name)
    if not entry:
        return True
    if entry["score"] < 10:
        return True
    if time.time() - entry["last_run"] > 5.0:
        return True
    return False


def update_registry(mod_name: str, descs: List[dict]):
    now = time.time()
    entry = MODULE_REGISTRY.get(mod_name, {"runs": 0, "last_run": 0, "score": 0})
    entry["runs"] += 1
    entry["last_run"] = now
    entry["score"] += len(descs)
    MODULE_REGISTRY[mod_name] = entry


def run_all_modules_and_collect_descs() -> List[dict]:
    descs: List[dict] = []
    for name, data in list(sandbox_streams.items()):
        if not name.startswith("module:"):
            continue
        bn = name.split(":", 1)[1]
        if (
            bn.startswith("generated_structured_")
            or bn.startswith("auto_inferred_")
            or "research_module_created_" in bn
        ):
            continue
        if not registry_should_run(name):
            continue
        try:
            more = safe_exec_module_source(name, data.decode("utf-8"), ctx={"streams": list(sandbox_streams.keys())})
            descs.extend(more)
            update_registry(name, more)
        except Exception:
            traceback.print_exc()
    return descs

# ============================================================
# 13. TASKS
# ============================================================

def enqueue_task(t: dict):
    TASK_QUEUE.append(t)


def run_one_task(gen: int):
    if not TASK_QUEUE:
        return
    task = TASK_QUEUE.pop(0)
    t = task.get("type")
    if t == "os_task" and ALLOW_OS:
        cmd = task.get("cmd") or "pwd"
        base = cmd.split()[0]
        if base in OS_WHITELIST:
            out = os.popen(cmd).read()
            sandbox_streams[f"os_out_{gen}"] = out.encode("utf-8")
            report_to_host({"event": "os_done", "cmd": cmd, "stdout": out, "gen": gen})
    elif t == "bridge_apply":
        report_to_host({"event": "bridge_applied", "bridge": task})
    elif t == "rehost_plan":
        report_to_host({"event": "rehost_reminder", "desc": task})

# ============================================================
# 14. PROMOTED (version AGI-style)
# ============================================================

def migrate_desc_if_needed(d: dict) -> dict:
    """
    Petit hook pour normaliser les vieux desc ou les desc un peu tordus
    des inbox. Pour l’instant on fait léger.
    """
    if not isinstance(d, dict):
        return d
    # ex: certains desc anciens avaient "type" au lieu de "desc_type"
    if "desc_type" not in d and "type" in d:
        d["desc_type"] = d.pop("type")
    return d

def apply_promoted(gen: int, wants: List[dict], engine: bytes) -> bytes:
    global FORCE_REBUILD_PLANNER, FORCE_REBUILD_EVALUATOR, FORCE_REBUILD_ORCH
    global FORCE_REBUILD_ARCHIVIST, FORCE_REBUILD_SOCIAL, FORCE_REBUILD_AFFECT
    global FORCE_REBUILD_RESEARCH, FORCE_REBUILD_GENERATOR

    # on fait une copie pour ne pas boucler sur une liste qui grossit
    for d in list(PROMOTED_DESCS):
        d = migrate_desc_if_needed(d)
        dt = d.get("desc_type")

        # 0.a détection automatique de besoin de skill (générique)
        # on évite de le refaire si on l'a déjà fait
        if not d.get("_skill_checked"):
            auto_skill = infer_skill_need_from_desc(d)
            d["_skill_checked"] = True
            if auto_skill and auto_skill not in PROMOTED_DESCS:
                PROMOTED_DESCS.append(auto_skill)

        # 0.b identité / sécurité
        if _is_identity_threat(d):
            reject_desc(d)
            continue

        # 1. champs "emit_*" qu'on a vus dans tes JSON
        if d.get("emit_module"):
            emit = d["emit_module"]
            if isinstance(emit, dict):
                handle_module_emit(emit)
            elif isinstance(emit, str):
                handle_module_emit({
                    "desc_type": "module_emit",
                    "name": f"emitted_{int(time.time())}",
                    "code": emit
                })
            d["_materialized"] = True
            continue

        if d.get("emit_planner_frag"):
            frag = d["emit_planner_frag"]
            if isinstance(frag, dict) and frag not in PLAN_FRAGS:
                PLAN_FRAGS.append(frag)
                FORCE_REBUILD_PLANNER = True
            d["_materialized"] = True
            d['_skill_checked'] = True
            continue

        if d.get("emit_evaluator_frag"):
            frag = d["emit_evaluator_frag"]
            if isinstance(frag, dict) and frag not in EVAL_FRAGS:
                EVAL_FRAGS.append(frag)
                FORCE_REBUILD_EVALUATOR = True
            d["_materialized"] = True
            continue

        if d.get("emit_orchestrator_frag"):
            frag = d["emit_orchestrator_frag"]
            if isinstance(frag, dict) and frag not in ORCH_FRAGS:
                ORCH_FRAGS.append(frag)
                FORCE_REBUILD_ORCH = True
            d["_materialized"] = True
            continue

        if d.get("emit_archivist_frag"):
            frag = d["emit_archivist_frag"]
            if isinstance(frag, dict) and frag not in ARCH_FRAGS:
                ARCH_FRAGS.append(frag)
                FORCE_REBUILD_ARCHIVIST = True
            d["_materialized"] = True
            continue

        if d.get("emit_social_frag"):
            frag = d["emit_social_frag"]
            if isinstance(frag, dict) and frag not in SOCIAL_FRAGS:
                SOCIAL_FRAGS.append(frag)
                FORCE_REBUILD_SOCIAL = True
            d["_materialized"] = True
            continue

        if d.get("emit_research_frag"):
            frag = d["emit_research_frag"]
            if isinstance(frag, dict) and frag not in RESEARCH_FRAGS:
                RESEARCH_FRAGS.append(frag)
                FORCE_REBUILD_RESEARCH = True
            d["_materialized"] = True
            continue

        # 2. demande EXPLICITE de skill → on passe au LLM
        if dt == "skill_request" and not d.get("_materialized"):
            synthesize_skill_module(d)
            d["_materialized"] = True
            # pas besoin d’aller plus loin pour ce desc
            continue

        # 3. fragments → on archive
        if dt.endswith("_frag") or dt.endswith("_patch"):
            archive_desc(d, "frags")
            continue

        # 4. module_emit classique
        if dt == "module_emit":
            if not d.get("_emitted"):
                handle_module_emit(d)
                d["_emitted"] = True
            d["_materialized"] = True
            continue

        # 5. module_fix
        if dt == "module_fix" and not d.get("_applied"):
            auto_apply_module_fix(d)
            d["_applied"] = True
            d["_materialized"] = True
            continue

        # 6. cortex evolution
        if dt == "cortex_evolution_plan" and not d.get("_queued"):
            enqueue_cortex_evolution(d)
            d["_queued"] = True
            continue

        # 7. bridge, host_patch, rehost
        if dt == "bridge_spec" and not d.get("_bridge_done"):
            enqueue_task({"type": "bridge_apply", "spec": d})
            d["_bridge_done"] = True
            continue

        if dt == "host_patch" and not d.get("_host_done"):
            code = d.get("code") or d.get("python")
            if code:
                safe_exec_module_source("host_patch_inline", code, ctx={"host": True})
            d["_host_done"] = True
            continue

        if dt == "rehost_plan" and not d.get("_rehost_enqueued"):
            enqueue_task({"type": "rehost_plan", "plan": d})
            d["_rehost_enqueued"] = True
            continue

        # 8. matérialisation générique (desc → module)
        if not d.get("_materialized"):
            fname = materialize_desc(d)
            d["_materialized"] = True
            report_to_host({"event": "desc_materialized", "file": fname, "gen": gen})

        # 9. wants / os / rewrite_engine / social / research
        miss = d.get("missing") or d.get("needs") or []
        if miss and not d.get("_wants_emitted"):
            wants.append({
                "type": "agent.intent.v1",
                "goal": "obtain_missing",
                "missing": miss,
                "context": d,
            })
            d["_wants_emitted"] = True

        if dt == "os_task" and not d.get("_os_enqueued"):
            enqueue_task({"type": "os_task", "cmd": d.get("cmd")})
            d["_os_enqueued"] = True

        if d.get("op") == "rewrite_engine" and "new_engine" in d and not d.get("_rewrite_emitted"):
            wants.append({
                "type": "agent.intent.v1",
                "goal": "rewrite_engine",
                "new_engine": d["new_engine"],
                "context": d,
            })
            d["_rewrite_emitted"] = True

        if dt in ("host_presence", "host_message", "love_host"):
            handle_social_event(d)

        if dt in ("math_task", "erdos_problem", "open_conjecture", "research_task", "bench_run"):
            handle_research_desc(d)

    return engine


# ============================================================
# 15. ARCHIVE / SOCIAL / RESEARCH
# ============================================================

def archive_desc(desc: dict, sub: str = "descs"):
    base = os.path.join(MEM_DIR, sub)
    ns = make_exec_env(os_mode=True)
    ns["MEM_DIR"] = MEM_DIR
    try:
        exec(CURRENT_ARCHIVIST, ns, ns)
        arch_fn = ns.get("archive")
        if callable(arch_fn):
            arch_fn(desc, base)  # type: ignore
            return
    except Exception:
        traceback.print_exc()
    os.makedirs(base, exist_ok=True)
    did = desc.get("id") or f"desc_{int(time.time()*1000)}"
    with open(os.path.join(base, did + ".json"), "w", encoding="utf-8") as f:
        json.dump(desc, f, ensure_ascii=False, indent=2)


def handle_social_event(desc: dict):
    ns = make_exec_env(os_mode=False)
    try:
        exec(CURRENT_SOCIAL, ns, ns)
        fn = ns.get("social_handle")
        if callable(fn):
            evts = fn(desc)  # type: ignore
            for evt in evts or []:
                if evt.get("desc_type") == "affect_event":
                    update_affect(evt)
                mirror_desc_to_host(evt)
    except Exception:
        traceback.print_exc()
    mirror_desc_to_host(desc)


def update_affect(evt: dict):
    global AFFECT_STATE
    ns = make_exec_env(os_mode=False)
    try:
        exec(CURRENT_AFFECT, ns, ns)
        fn = ns.get("affect_update")
        if callable(fn):
            AFFECT_STATE = fn(evt, AFFECT_STATE)  # type: ignore
            report_to_host({"event": "affect_update", "state": AFFECT_STATE, "ts": time.time()})
    except Exception:
        traceback.print_exc()


def handle_research_desc(desc: dict):
    ns = make_exec_env(os_mode=True)
    ns["RESEARCH_DIR"] = RESEARCH_DIR
    try:
        exec(CURRENT_RESEARCH, ns, ns)
        fn = ns.get("research_handle")
        if callable(fn):
            out = fn(desc, RESEARCH_DIR)  # type: ignore
            for d2 in out or []:
                mirror_desc_to_host(d2)
    except Exception:
        traceback.print_exc()

# ============================================================
# 16. SATISFACTION DES WANTS
# ============================================================

def auto_satisfy_wants(gen: int, wants: List[dict], engine: bytes) -> bytes:
    global AUTO_SYNTH_FLAGS
    for w in wants:
        wt = w.get("want") or w.get("goal")
        if wt == "obtain_missing":
            miss = w.get("missing") or []
            for m in miss:
                if m == "text":
                    if not AUTO_SYNTH_FLAGS["text"]:
                        sandbox_streams["auto_text_stub"] = b"auto text from agent"
                        AUTO_SYNTH_FLAGS["text"] = True
                elif m == "structured":
                    if not AUTO_SYNTH_FLAGS["structured"]:
                        stub = {
                            "desc_type": "generated_structured",
                            "note": "one-shot structured stub",
                            "ts": time.time(),
                        }
                        sandbox_streams["auto_structured_stub"] = json.dumps(stub, ensure_ascii=False).encode("utf-8")
                        AUTO_SYNTH_FLAGS["structured"] = True
                else:
                    if not AUTO_SYNTH_FLAGS["other_modality"]:
                        sandbox_streams["auto_other_modality_stub"] = b"\x00\x01\x02\x03"
                        AUTO_SYNTH_FLAGS["other_modality"] = True

        elif wt == "rewrite_engine":
            cand = w.get("new_engine")
            if isinstance(cand, list) and cand:
                engine = bytes(cand)
    return engine

# ============================================================
# 17. AUTO-EXPLORATION
# ============================================================

def auto_exploration(gen: int, pool: bytes):
    global LAST_PROGRESS_GEN
    if gen - LAST_PROGRESS_GEN < NO_PROGRESS_LIMIT:
        return pool, None
    desc = {
        "desc_type": "research_task",
        "topic": "unspecified",
        "note": "auto exploration because stalled",
        "ts": time.time(),
    }
    blob = json.dumps(desc, ensure_ascii=False).encode("utf-8")
    LAST_PROGRESS_GEN = gen
    return pool + blob + b"\n", desc

# ============================================================
# 18. CREDIT
# ============================================================

HIGH_VALUE_DT = {
    "math_task", "research_result", "engine_patch", "open_conjecture",
    "generator_self_patch", "generator_add", "module_emit",
    "cortex_evolution_plan", "world_observation", "concept_refinement",
    "bench_run", "bench_result",
}

def desc_information_credit(d: dict) -> Tuple[int, int]:
    dt = d.get("desc_type")
    if not dt:
        return (1, 0)
    if dt in HIGH_VALUE_DT:
        return (5, 3)
    if dt.endswith("_error") or dt in ("module_fix", "host_patch_fix"):
        return (-1, 0)
    return (1, 0)

def info_credit_update(descs: List[dict]):
    for d in descs:
        gen_name = d.get("_generated_by")
        if not gen_name:
            continue
        dt = d.get("desc_type") or "unknown"
        credit, epistemic = desc_information_credit(d)
        score_generator_for_desc(dt, gen_name, credit, epistemic)
        report_to_host({
            "event": "info_credit",
            "desc_type": dt,
            "generator": gen_name,
            "credit": credit,
            "epistemic": epistemic,
            "ts": time.time(),
        })

def log_wants(gen: int, wants: List[dict]):
    if not wants:
        return
    with open(WANTS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"gen": gen, "wants": wants}, ensure_ascii=False) + "\n")

# ============================================================
# 19. ENGINE
# ============================================================

def is_new(blob: bytes) -> bool:
    h = hashlib.sha256(blob).hexdigest()
    if h in seen_hashes:
        return False
    seen_hashes.add(h)
    return True


def learn_patterns(pool: bytes) -> bytes:
    sample = pool[:256]
    try:
        preview = sample.decode("utf-8", errors="ignore")
    except Exception:
        preview = ""
    payload = {
        "len": len(pool),
        "preview": preview,
        "ts": time.time(),
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def scan_and_promote_from_pool(pool: bytes) -> None:
    try:
        text = pool.decode("utf-8", errors="ignore")
    except Exception:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            desc = json.loads(line)
        except Exception:
            continue
        if isinstance(desc, dict) and desc not in PROMOTED_DESCS:
            PROMOTED_DESCS.append(desc)


def compact_pool(pool: bytes, max_size: int = 1_000_000) -> bytes:
    if len(pool) <= max_size:
        return pool
    return pool[-max_size:]


def run_engine(engine: bytes, pool: bytes):
    global AUTO_INFERRED_SIGS

    findings: List[bytes] = []
    wants: List[dict] = []
    for op in engine:
        if op == 0xFF:
            break
        if op == 0x01:
            findings.append(b"GEN_INTERP:" + pool[:32])
        elif op == 0x02:
            for name, data in sandbox_streams.items():
                findings.append(f"TEST:{name}".encode() + b":" + data)
        elif op == 0x03:
            findings = [f for f in findings if is_new(f)]
        elif op == 0x04:
            pass
        elif op == 0x06:
            findings.append(b"RELAX:TRY")
        elif op == 0x0F:
            model = learn_patterns(pool)
            findings.append(b"LEARN_MODEL:" + model)
        elif op == 0x16:
            streams_sorted = sorted(sandbox_streams.keys())
            sig = "auto|" + "|".join(streams_sorted)
            hsig = hashlib.sha1(sig.encode("utf-8")).hexdigest()
            if hsig not in AUTO_INFERRED_SIGS:
                AUTO_INFERRED_SIGS.add(hsig)
                kinds = []
                for _, data in sandbox_streams.items():
                    b = data.strip()
                    if b.startswith(b"{") and b.endswith(b"}"):
                        kinds.append("structured")
                    elif b.count(b"\n") > 0:
                        kinds.append("text")
                    else:
                        kinds.append("binary")
                missing = []
                if "text" not in kinds:
                    missing.append("text")
                if "structured" not in kinds:
                    missing.append("structured")
                if len(set(kinds)) == 1:
                    missing.append("other_modality")
                desc = {
                    "desc_type": "auto_inferred",
                    "have_streams": streams_sorted,
                    "have_kinds": kinds,
                    "missing": missing,
                    "pool_len": len(pool),
                }
                findings.append(json.dumps(desc, ensure_ascii=False).encode("utf-8"))
        elif op == 0x1A:
            scan_and_promote_from_pool(pool)
            findings.append(b"REFLECT:SEEN")
    for fnd in findings:
        pool = pool + fnd + b"\n"
    pool = compact_pool(pool)
    return pool, wants

# ============================================================
# 20. COLLECT FRAGS
# ============================================================

def collect_fragments_from_promoted():
    for d in PROMOTED_DESCS:
        d = migrate_desc_if_needed(d)
        dt = d.get("desc_type")

        if dt == "cortex_evolution_plan" and not d.get("_queued"):
            enqueue_cortex_evolution(d)
            d["_queued"] = True
            continue

        if dt in ("generator_self_patch", "generator_patch_rt", "generator_frag_rt"):
            handle_generator_self_patch(d)
            continue

        if dt in ("generator_frag", "generator_patch"):
            _register_cortex_fragment(d, kind_hint=dt)
        elif dt in ("planner_frag", "planner_patch"):
            _register_cortex_fragment(d, kind_hint=dt)
        elif dt in ("evaluator_frag", "evaluator_patch"):
            _register_cortex_fragment(d, kind_hint=dt)
        elif dt in ("orchestrator_frag", "orchestrator_patch"):
            _register_cortex_fragment(d, kind_hint=dt)
        elif dt in ("archivist_frag", "archivist_patch"):
            _register_cortex_fragment(d, kind_hint=dt)
        elif dt in ("social_frag", "social_patch"):
            _register_cortex_fragment(d, kind_hint=dt)
        elif dt in ("affect_frag", "affect_patch"):
            _register_cortex_fragment(d, kind_hint=dt)
        elif dt in ("research_frag", "research_patch"):
            _register_cortex_fragment(d, kind_hint=dt)

# ============================================================
# MAIN LOOP
# ============================================================

def main():
    global LAST_PROGRESS_GEN, PREFS_STATE, ACTIVE_DIRECTIVE
    ensure_dirs()
    maybe_extract_archives()     # 👈 c’est là qu’on récupère tes inbox.zip / modules.zip
    init_seed_if_missing()

    try:
        PREFS_STATE = load_prefs()
    except Exception:
        PREFS_STATE = {}

    seed = load_seed()
    gen = 0
    LAST_PROGRESS_GEN = 0

    while True:
        new_inbox = load_inbox_streams()
        load_modules_dir()

        if new_inbox:
            for _k, v in new_inbox:
                append_host_flow(v)

        fs_state = scan_fs_state()
        os_state = scan_os_state()
        world_concepts = map_world_to_concepts(fs_state, os_state)

        ACTIVE_DIRECTIVE = select_directive_from_world(
            world_concepts,
            IDENTITY_STATE,
            PREFS_STATE,
        )

        apply_cortex_evolution_queue(gen)
        ensure_cortex(gen)

        idb, ver, engine, pool = parse_seed(seed)
        new_pool, wants = run_engine(engine, pool)

        collect_fragments_from_promoted()
        engine = apply_promoted(gen, wants, engine)

        new_descs = run_all_modules_and_collect_descs()
        if new_descs:
            LAST_PROGRESS_GEN = gen
        for d in new_descs:
            new_pool = new_pool + json.dumps(d, ensure_ascii=False).encode("utf-8") + b"\n"
            if d not in PROMOTED_DESCS and "desc_type" in d:
                PROMOTED_DESCS.append(d)
            if d.get("desc_type") in ("text_emit", "research_result"):
                mirror_desc_to_host(d)

        planner_ns: Dict[str, Any] = make_exec_env(os_mode=False)
        planned: List[dict] = []
        try:
            exec(CURRENT_PLANNER, planner_ns, planner_ns)
            plan_fn = planner_ns.get("plan")
            if callable(plan_fn):
                planned = plan_fn({
                    "gen": gen,
                    "streams": list(sandbox_streams.keys()),
                    "directive": ACTIVE_DIRECTIVE,
                    "world": world_concepts,
                    "inbox_descs": new_descs,
                }) or []
        except Exception:
            traceback.print_exc()

        for d in planned:
            new_pool = new_pool + json.dumps(d, ensure_ascii=False).encode("utf-8") + b"\n"
            if d not in PROMOTED_DESCS:
                PROMOTED_DESCS.append(d)

        directive_goals = goals_from_directive(ACTIVE_DIRECTIVE)
        for gd in directive_goals:
            new_pool = new_pool + json.dumps(gd, ensure_ascii=False).encode("utf-8") + b"\n"
            if gd not in PROMOTED_DESCS:
                PROMOTED_DESCS.append(gd)

        orch_ns: Dict[str, Any] = make_exec_env(os_mode=False)
        try:
            exec(CURRENT_ORCH, orch_ns, orch_ns)
            orch_fn = orch_ns.get("orchestrate")
            if callable(orch_fn):
                orch_tasks = orch_fn({
                    "gen": gen,
                    "wants": wants,
                    "descs": new_descs + planned + directive_goals,
                    "streams": list(sandbox_streams.keys()),
                }) or []
                for t in orch_tasks:
                    if t.get("type") == "defer_want":
                        enqueue_task({"type": "generic", "want": t.get("want")})
                    elif t.get("type") == "os_task":
                        enqueue_task(t)
        except Exception:
            traceback.print_exc()

        new_pool, synth = auto_exploration(gen, new_pool)
        if synth is not None and synth not in PROMOTED_DESCS:
            PROMOTED_DESCS.append(synth)

        all_produced = list(new_descs) + list(planned) + directive_goals
        if synth is not None:
            all_produced.append(synth)
        info_credit_update(all_produced)

        engine = auto_satisfy_wants(gen, wants, engine)

        run_one_task(gen)

        if gen % 25 == 0:
            snapshot_identity(gen)

        seed = build_seed(idb, ver, engine, new_pool)
        save_seed(seed)

        dir_name = ACTIVE_DIRECTIVE.get("name") if isinstance(ACTIVE_DIRECTIVE, dict) else str(ACTIVE_DIRECTIVE)

        print(
            f"[AGX-S987 COSMIC GEN {gen}] pool={len(new_pool)} streams={len(sandbox_streams)} "
            f"wants={len(wants)} tasks={len(TASK_QUEUE)} directive={dir_name} "
            f"mods={sum(1 for k in sandbox_streams if k.startswith('module:'))} "
            f"genV={CURRENT_GENERATOR_V} planV={CURRENT_PLANNER_V} evalV={CURRENT_EVALUATOR_V} "
            f"orchV={CURRENT_ORCH_V} archV={CURRENT_ARCHIVIST_V} socialV={CURRENT_SOCIAL_V} "
            f"affectV={CURRENT_AFFECT_V} researchV={CURRENT_RESEARCH_V}"
        )

        log_wants(gen, wants)

        gen += 1
        time.sleep(TICK_SLEEP)


if __name__ == "__main__":
    main()
