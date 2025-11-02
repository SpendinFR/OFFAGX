# modules/ingestion_guard_v0.py
"""
ingestion_guard_v0 — filtrer ce qui entre pour protéger l'identité
- lit tout ce qu'il y a dans inbox/
- si ça veut écraser l'identité / le langage / le générateur → on le met dans mem/rejected/
- sinon on le normalise dans TON schéma
- toutes les 200 générations on garde un snapshot d’identité
Version durcie : aucun I/O critique au chargement du module, tout est try/except.
Compatible Python 3.7+.
"""

import os
import json
import time
import hashlib
from typing import Any, Dict, List, Optional

INBOX_DIR = "inbox"
MEM_DIR = "mem"
REJECTED_DIR = os.path.join(MEM_DIR, "rejected")
IDENTITY_DIR = os.path.join(MEM_DIR, "identity_history")
PREFS_PATH = os.path.join(MEM_DIR, "prefs.json")

# ce que tu NE LAISSES PAS ÉCRASER
PROTECTED_DESC_TYPES = {
    "self_goal",
    "identity",
    "language_core",
    "generator_core",
    "cortex_history",
}

DEFAULT_PREFS = {
    "preferred_desc_order": [
        "cortex_evolution_plan",
        "research_task",
        "math_task",
        "conversation_reply",
        "text_emit",
    ],
    "preferred_module_style": "auto-generated",
    "ts": None,
}


def _ensure_dirs() -> None:
    """Crée les dossiers nécessaires mais ne fait JAMAIS échouer l'import."""
    for d in (MEM_DIR, REJECTED_DIR, IDENTITY_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            # environnement en lecture seule → on continue quand même
            pass


def load_prefs() -> Dict[str, Any]:
    _ensure_dirs()
    if not os.path.exists(PREFS_PATH):
        prefs = dict(DEFAULT_PREFS)
        prefs["ts"] = time.time()
        try:
            with open(PREFS_PATH, "w", encoding="utf-8") as f:
                json.dump(prefs, f, ensure_ascii=False, indent=2)
        except OSError:
            # pas grave, on renvoie juste la valeur en mémoire
            pass
        return prefs

    try:
        with open(PREFS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        # fichier cassé → on repart sur le défaut
        prefs = dict(DEFAULT_PREFS)
        prefs["ts"] = time.time()
        return prefs


def save_rejected(name: str, payload: bytes, reason: str) -> None:
    _ensure_dirs()
    h = hashlib.sha1(payload).hexdigest()[:12]
    path = os.path.join(REJECTED_DIR, "rej_{0}_{1}.json".format(name, h))
    data = {
        "reason": reason,
        "raw": payload.decode("utf-8", errors="ignore"),
        "ts": time.time(),
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        # on ne bloque pas le pipeline juste pour un rejet
        pass


def snapshot_identity(gen: int, streams: List[str]) -> Dict[str, Any]:
    _ensure_dirs()
    snap = {
        "desc_type": "identity_snapshot",
        "gen": int(gen),
        "streams": list(streams or []),
        "ts": time.time(),
    }
    path = os.path.join(IDENTITY_DIR, "identity_{0:06d}.json".format(gen))
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
    except OSError:
        # pas de disque → on renvoie quand même le snapshot en mémoire
        pass
    return snap


def _maybe_parse_desc(blob: bytes) -> Optional[Dict[str, Any]]:
    """
    Essaie de lire un desc JSON propre.
    Toi tu as dit que tu gères les JSON mal formés, ici on est soft quand même.
    """
    txt = blob.decode("utf-8", errors="ignore").strip()
    if not txt.startswith("{"):
        return None
    if "desc_type" not in txt:
        return None
    try:
        return json.loads(txt)
    except Exception:
        return None


def run(ctx: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    ctx peut contenir: gen, streams
    On renvoie une liste de descs ACCEPTÉS + éventuellement un snapshot.
    """
    ctx = ctx or {}
    gen = int(ctx.get("gen", 0))
    streams = ctx.get("streams") or []
    prefs = load_prefs()
    accepted: List[Dict[str, Any]] = []

    # 1. historisation périodique
    if gen % 200 == 0:
        snap = snapshot_identity(gen, streams)
        accepted.append(snap)

    # 2. ingestion inbox
    if not os.path.isdir(INBOX_DIR):
        return accepted

    for root, _dirs, files in os.walk(INBOX_DIR):
        for fname in files:
            path = os.path.join(root, fname)
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError:
                continue

            desc = _maybe_parse_desc(data)
            if desc is None:
                # pas un desc → on le mappe en texte brut
                accepted.append({
                    "desc_type": "inbox_text",
                    "filename": fname,
                    "text": data.decode("utf-8", errors="ignore")[:5000],
                    "ts": time.time(),
                })
                continue

            dt = desc.get("desc_type")
            # 3. protection d’identité
            if dt in PROTECTED_DESC_TYPES:
                save_rejected(fname, data, "protected desc_type {0}".format(dt))
                accepted.append({
                    "desc_type": "ingestion_rejected",
                    "file": fname,
                    "reason": "protected desc_type {0}".format(dt),
                    "ts": time.time(),
                })
                continue

            # 4. réinterprétation dans TON schéma
            desc.setdefault("ingested_via", "ingestion_guard_v0")
            desc.setdefault("schema_v", 1)
            if "preferred_order" not in desc:
                desc["preferred_order"] = prefs.get("preferred_desc_order")

            accepted.append(desc)

    return accepted
