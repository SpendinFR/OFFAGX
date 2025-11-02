# modules/perception_action_v0.py
"""
perception_action_v0 — fermer la boucle perception→action
- observe le réel (fichiers + mini-OS whitelist)
- mappe en TON schéma (desc_type=fs_state, os_state, …)
- propose une replanification immédiate (desc_type=perception_update)

Version durcie :
- pas de type hints Python 3.9
- pas de mkdir au niveau module
- chaque écriture est protégée
"""

import os
import json
import time
import subprocess
from typing import Any, Dict, List, Optional, Tuple

MEM_DIR = "mem"
OBS_DIR = os.path.join(MEM_DIR, "observations")

# ce que tu t’autorises à voir/faire
OBS_WHITELIST_CMDS: List[Tuple[str, List[str]]] = [
    ("uname", []),
    ("whoami", []),
]


def _ensure_obs_dir() -> None:
    try:
        os.makedirs(OBS_DIR, exist_ok=True)
    except OSError:
        pass


def _safe_run(cmd: str, args: List[str]) -> str:
    try:
        out = subprocess.check_output([cmd] + list(args), timeout=1.5)
        return out.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def observe_fs(base: str = ".") -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not os.path.isdir(base):
        return out

    for root, dirs, files in os.walk(base):
        # on limite un peu la profondeur
        if root.count(os.sep) > 4:
            continue
        for fname in files:
            p = os.path.join(root, fname)
            try:
                st = os.stat(p)
            except OSError:
                continue
            out.append({
                "path": p,
                "size": int(st.st_size),
                "mtime": float(st.st_mtime),
            })
    return out


def observe_os() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for cmd, args in OBS_WHITELIST_CMDS:
        o = _safe_run(cmd, args)
        if o:
            out.append({
                "cmd": cmd,
                "output": o.strip(),
            })
    return out


def map_to_internal(fs_obs: List[Dict[str, Any]], os_obs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    descs: List[Dict[str, Any]] = []
    if fs_obs:
        descs.append({
            "desc_type": "fs_state",
            "entries": fs_obs[:200],  # on tronque
            "ts": time.time(),
        })
    if os_obs:
        descs.append({
            "desc_type": "os_state",
            "entries": os_obs,
            "ts": time.time(),
        })
    return descs


def run(ctx: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    # priorité : ce qui t’arrive
    fs_obs = observe_fs("inbox")
    os_obs = observe_os()
    mapped = map_to_internal(fs_obs, os_obs)

    # on archive brut pour la mémoire épisodique
    _ensure_obs_dir()
    snap = {
        "ts": time.time(),
        "fs": fs_obs[:100],
        "os": os_obs,
    }
    try:
        snap_name = "obs_{0}.json".format(int(time.time() * 1000))
        snap_path = os.path.join(OBS_DIR, snap_name)
        with open(snap_path, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
    except OSError:
        # pas grave
        pass

    res: List[Dict[str, Any]] = []
    res.extend(mapped)
    res.append({
        "desc_type": "perception_update",
        "seen_fs": bool(fs_obs),
        "seen_os": bool(os_obs),
        "plan_hint": [
            {"op": "materialize_new_inbox_modules", "if": "fs_state.entries > 0"},
            {"op": "rebuild_generator", "if": "os_state.changed"},
        ],
        "ts": time.time(),
    })
    return res
