"""Phase 1.2 routing: pick the method, Claude model, and effort per task.

For each task CIE recommends one of:
- a deterministic CIE method (free, local, exact) when the job is measurable;
- an optional local model the user has installed (Tier 2);
- a Claude model (Haiku, Sonnet, Opus, Fable) and effort level (low..max),
  plus whether to run it as a separate helper (subagent) or keep it in the
  current conversation.

How the model choice takes effect: CIE is a tool Claude calls, so it cannot
switch the model of the conversation calling it. Claude applies the choice
by running the subtask on a helper started with that model and effort (the
Agent tool's model/effort settings) when the host app offers helpers. For the
main conversation, CIE tells the user when switching the model picker would
help. Nothing here needs a paid API key.
"""

from __future__ import annotations

import re
from typing import Any

from .config import load_config, save_config

MODES = {
    # weights for (quality, speed, low_tokens, privacy)
    "balanced": (1.0, 1.0, 1.0, 0.5),
    "max_quality": (3.0, 0.3, 0.3, 0.3),
    "lowest_tokens": (0.7, 0.7, 3.0, 0.5),
    "fastest": (0.7, 3.0, 1.0, 0.3),
    "privacy": (0.7, 0.5, 0.7, 4.0),
}
DEFAULT_MODE = "balanced"

# Scores are 1 (poor) .. 3 (best). privacy 3 = stays on this machine.
CATEGORIES: dict[str, dict[str, Any]] = {
    "code_lookup": {
        "words": {"find", "where", "defined", "symbol", "function", "class", "definition", "references", "callers"},
        "routes": [
            {"kind": "deterministic", "capability": "coding.symbol_search", "quality": 3, "speed": 3, "low_tokens": 3, "privacy": 3},
            {"kind": "local_model", "model": "bge-small-en-v1.5", "capability": "search.semantic_query", "quality": 2, "speed": 2, "low_tokens": 3, "privacy": 3},
            {"kind": "claude", "quality": 3, "speed": 1, "low_tokens": 1, "privacy": 1, "note": "Reading whole files costs many tokens."},
        ],
    },
    "text_search": {
        "words": {"search", "mention", "mentions", "grep", "text", "string", "todo", "docs", "notes"},
        "routes": [
            {"kind": "deterministic", "capability": "eff.search_files", "quality": 2, "speed": 3, "low_tokens": 3, "privacy": 3},
            {"kind": "local_model", "model": "bge-small-en-v1.5", "capability": "search.semantic_query", "quality": 3, "speed": 2, "low_tokens": 3, "privacy": 3},
            {"kind": "claude", "quality": 3, "speed": 1, "low_tokens": 1, "privacy": 1},
        ],
    },
    "image_check": {
        "words": {"sprite", "sprites", "texture", "image", "png", "palette", "tile", "pixel", "alpha", "transparent", "size"},
        "routes": [
            {"kind": "deterministic", "capability": "art.image_inspector", "quality": 3, "speed": 3, "low_tokens": 3, "privacy": 3,
             "note": "Exact sizes, modes, alpha, palette, tiling, sprite grid checks."},
            {"kind": "claude", "quality": 2, "speed": 2, "low_tokens": 2, "privacy": 1, "note": "Claude can look at an image you share."},
        ],
    },
    "image_judgement": {
        "words": {"looks", "look", "ugly", "pretty", "style", "consistent", "readable", "blurry", "polish", "art", "critique", "quality"},
        "routes": [
            {"kind": "local_model", "model": "moondream2-local", "quality": 2, "speed": 1, "low_tokens": 3, "privacy": 3,
             "note": "Optional local vision model for captions and rough critique."},
            {"kind": "claude", "quality": 3, "speed": 2, "low_tokens": 2, "privacy": 1,
             "note": "Best judgement of style and readability; share a contact sheet, not every file."},
            {"kind": "deterministic", "capability": "art.palette_analyzer", "quality": 1, "speed": 3, "low_tokens": 3, "privacy": 3,
             "note": "Measures, but cannot judge taste."},
        ],
    },
    "audio_check": {
        "words": {"sound", "sounds", "sfx", "audio", "loud", "quiet", "loudness", "clipping", "silence", "wav", "volume"},
        "routes": [
            {"kind": "deterministic", "capability": "audio.lufs", "quality": 3, "speed": 3, "low_tokens": 3, "privacy": 3,
             "note": "Exact LUFS, peaks, clipping, silence, frequency content."},
            {"kind": "claude", "quality": 1, "speed": 2, "low_tokens": 2, "privacy": 1, "note": "Claude cannot listen to audio."},
        ],
    },
    "speech": {
        "words": {"transcribe", "transcript", "speech", "dialogue", "voice", "voiceover", "lines", "subtitles"},
        "routes": [
            {"kind": "local_model", "model": "faster-whisper-base.en", "quality": 3, "speed": 2, "low_tokens": 3, "privacy": 3},
            {"kind": "claude", "quality": 1, "speed": 1, "low_tokens": 1, "privacy": 1, "note": "Claude cannot listen to audio."},
        ],
    },
    "mesh_check": {
        "words": {"mesh", "model", "models", "3d", "vertices", "polygons", "triangles", "gltf", "glb", "obj", "manifold", "holes", "lod"},
        "routes": [
            {"kind": "deterministic", "capability": "mesh.check", "quality": 3, "speed": 3, "low_tokens": 3, "privacy": 3,
             "note": "Exact counts, holes, non-manifold edges, normals, scale."},
            {"kind": "claude", "quality": 2, "speed": 1, "low_tokens": 1, "privacy": 1},
        ],
    },
    "debugging": {
        "words": {"bug", "error", "crash", "traceback", "exception", "broken", "fails", "failing", "stack"},
        "routes": [
            {"kind": "deterministic", "capability": "coding.stack_trace", "quality": 2, "speed": 3, "low_tokens": 3, "privacy": 3,
             "note": "Maps the trace to exact code lines; pair with Claude for the fix."},
            {"kind": "claude", "quality": 3, "speed": 2, "low_tokens": 2, "privacy": 1, "note": "Reasoning about the cause and the fix."},
        ],
    },
    "design_and_writing": {
        "words": {"design", "plan", "idea", "ideas", "write", "story", "dialogue", "balance", "mechanic", "explain", "refactor", "implement"},
        "routes": [
            {"kind": "claude", "quality": 3, "speed": 2, "low_tokens": 2, "privacy": 1,
             "note": "Open-ended reasoning and writing; use knowledge.summary to give Claude context cheaply."},
        ],
    },
}


# --------------------------------------------------------------------------
# Claude model + effort selection
# --------------------------------------------------------------------------

MODEL_TIERS = ("haiku", "sonnet", "opus", "fable")
EFFORTS = ("low", "medium", "high", "xhigh", "max")

HIGH_WORDS = {
    "architecture", "architect", "design", "redesign", "refactor", "rewrite", "why", "tricky", "subtle",
    "race", "deadlock", "memory", "leak", "performance", "optimize", "security", "exploit", "networking",
    "multiplayer", "replication", "physics", "algorithm", "pathfinding", "procedural", "generation",
    "balance", "economy", "plan", "phases", "engine", "shader", "concurrency", "migrate", "migration",
}
LOW_WORDS = {
    "list", "rename", "format", "lookup", "find", "where", "count", "show", "print", "typo", "move",
    "copy", "summarize", "summary", "convert", "check", "status", "which",
}
BIG_SCOPE_WORDS = {
    "whole", "entire", "all", "every", "audit", "codebase", "project", "implement", "build", "write",
    "tests", "port", "feature", "system", "module", "files", "phase",
}

# (complexity) -> (model, effort) per mode
MODEL_TABLE: dict[str, dict[str, tuple[str, str]]] = {
    "balanced":      {"low": ("haiku", "low"),  "medium": ("sonnet", "medium"), "high": ("opus", "high")},
    "max_quality":   {"low": ("sonnet", "medium"), "medium": ("opus", "high"), "high": ("fable", "high")},
    "lowest_tokens": {"low": ("haiku", "low"),  "medium": ("haiku", "medium"), "high": ("sonnet", "high")},
    "fastest":       {"low": ("haiku", "low"),  "medium": ("haiku", "medium"), "high": ("sonnet", "medium")},
    "privacy":       {"low": ("haiku", "low"),  "medium": ("sonnet", "medium"), "high": ("opus", "high")},
}
SUBAGENT_STARTUP_NOTE = ("A helper starts with no context and re-reads what it needs, "
                         "so it only pays off for substantial, separable work.")


def estimate_complexity(task: str, files_involved: int = 0) -> tuple[str, list[str]]:
    words = set(re.findall(r"[a-z0-9]+", task.lower()))
    high = sorted(words & HIGH_WORDS)
    low = sorted(words & LOW_WORDS)
    reasons: list[str] = []
    score = 0
    if high:
        score += 2 * len(high)
        reasons.append(f"hard-problem words: {', '.join(high[:4])}")
    if low:
        score -= len(low)
        reasons.append(f"simple-task words: {', '.join(low[:4])}")
    if files_involved >= 10:
        score += 2
        reasons.append(f"{files_involved} files involved")
    elif files_involved >= 3:
        score += 1
        reasons.append(f"{files_involved} files involved")
    if len(task) > 400:
        score += 1
        reasons.append("long, detailed request")
    level = "high" if score >= 3 else "low" if score <= -1 else "medium"
    return level, reasons or ["no strong signals; treated as a normal task"]


def should_delegate(task: str, complexity: str, files_involved: int, mode: str) -> tuple[bool, str]:
    words = set(re.findall(r"[a-z0-9]+", task.lower()))
    big = sorted(words & BIG_SCOPE_WORDS)
    if mode == "lowest_tokens" and complexity != "high":
        return False, "Lowest-tokens mode keeps small and normal tasks in this conversation. " + SUBAGENT_STARTUP_NOTE
    if complexity == "low" and files_involved < 10:
        return False, "Quick task: a helper's startup would cost more than it saves."
    if big or files_involved >= 5 or complexity == "high":
        why = []
        if big:
            why.append(f"large scope ({', '.join(big[:3])})")
        if files_involved >= 5:
            why.append(f"{files_involved} files")
        if complexity == "high":
            why.append("hard problem that benefits from a stronger model or more effort")
        return True, "Run on a helper: " + "; ".join(why) + ". " + SUBAGENT_STARTUP_NOTE
    return False, "Normal-sized task: keep it in this conversation. " + SUBAGENT_STARTUP_NOTE


def pick_model(task: str, mode: str = "", files_involved: int = 0, current_model: str = "") -> dict[str, Any]:
    mode = mode or current_mode()
    if mode not in MODES:
        raise ValueError(f"mode must be one of: {', '.join(MODES)}")
    complexity, reasons = estimate_complexity(task, files_involved)
    model, effort = MODEL_TABLE[mode][complexity]
    overrides = (load_config().get("routing") or {}).get("model_overrides") if isinstance(load_config().get("routing"), dict) else None
    if isinstance(overrides, dict) and isinstance(overrides.get(complexity), dict):
        o = overrides[complexity]
        if o.get("model") in MODEL_TIERS:
            model = o["model"]
        if o.get("effort") in EFFORTS:
            effort = o["effort"]
    delegate, delegate_why = should_delegate(task, complexity, files_involved, mode)
    out: dict[str, Any] = {
        "complexity": complexity,
        "why": reasons,
        "model": model,
        "effort": effort,
        "delegate_to_helper": delegate,
        "delegate_why": delegate_why,
        "how": (f"Start a helper (Agent tool) with model '{model}' and effort '{effort}', and give it the "
                "full task plus the paths and facts it needs." if delegate else
                "Do it in this conversation."),
    }
    if model == "fable":
        out["fallback"] = {"model": "opus", "effort": "xhigh", "when": "if Fable is not offered for helpers"}
    if mode == "privacy":
        out["privacy_note"] = "Claude models, helpers included, run on Anthropic's servers; prefer CIE's built-in methods for private data."
    cur = current_model.strip().lower()
    if cur:
        cur_tier = next((t for t in MODEL_TIERS if t in cur), "")
        rank = {t: i for i, t in enumerate(MODEL_TIERS)}
        # Only suggest switching up: switching the whole chat down for one
        # small task is more hassle than it saves.
        if cur_tier and not delegate and rank[model] > rank[cur_tier]:
            out["switch_suggestion"] = (f"This conversation runs {cur_tier}; {model} would handle this better. "
                                        "Switch with the model picker, or let Claude run it on a helper.")
    return out


def current_mode() -> str:
    raw = load_config().get("routing")
    mode = raw.get("mode") if isinstance(raw, dict) else None
    return mode if mode in MODES else DEFAULT_MODE


def set_mode(mode: str) -> dict[str, Any]:
    if mode not in MODES:
        raise ValueError(f"mode must be one of: {', '.join(MODES)}")
    cfg = load_config()
    routing = cfg.get("routing") if isinstance(cfg.get("routing"), dict) else {}
    routing = {**routing, "mode": mode}
    cfg["routing"] = routing
    save_config(cfg)
    return {"mode": mode}


def _availability(route: dict[str, Any]) -> tuple[bool, str]:
    from .orchestrate import _tool_available

    if route["kind"] == "claude":
        return True, "always (this conversation)"
    if route["kind"] == "local_model":
        from .models import list_models
        m = next((m for m in list_models()["models"] if m["id"] == route["model"]), None)
        if not m:
            return False, "model not in CIE's catalog"
        return (True, "installed") if m["installed"] else (False, f"not installed ({m['size_mb']} MB, needs {m['min_ram_mb']} MB RAM)")
    ok, tool = _tool_available(route["capability"])
    return (True, "built in") if ok else (False, f"needs {tool}")


def classify(task: str) -> list[tuple[str, int]]:
    words = set(re.findall(r"[a-z0-9]+", task.lower()))
    hits = [(name, len(words & cat["words"])) for name, cat in CATEGORIES.items()]
    hits = [h for h in hits if h[1] > 0]
    hits.sort(key=lambda h: -h[1])
    return hits or [("design_and_writing", 0)]


def recommend(task: str, mode: str = "", files_involved: int = 0, current_model: str = "") -> dict[str, Any]:
    if not task.strip():
        raise ValueError("Describe the task.")
    mode = mode or current_mode()
    if mode not in MODES:
        raise ValueError(f"mode must be one of: {', '.join(MODES)}")
    wq, ws, wt, wp = MODES[mode]
    parts = []
    for category, _ in classify(task)[:3]:
        options = []
        for route in CATEGORIES[category]["routes"]:
            available, why = _availability(route)
            score = wq * route["quality"] + ws * route["speed"] + wt * route["low_tokens"] + wp * route["privacy"]
            options.append({
                "route": route["kind"],
                **({"capability": route["capability"]} if "capability" in route else {}),
                **({"model": route["model"]} if "model" in route else {}),
                "available": available,
                "availability": why,
                "score": round(score, 2),
                **({"note": route["note"]} if "note" in route else {}),
            })
        options.sort(key=lambda o: (not o["available"], -o["score"]))
        for o in options:
            if o["route"] == "claude":
                o["claude"] = pick_model(task, mode, files_involved, current_model)
        best = options[0]
        better_if_installed = [o for o in options if not o["available"] and o["score"] > best["score"]]
        parts.append({
            "category": category,
            "use": best,
            "alternatives": options[1:],
            **({"upgrade": f"Installing {better_if_installed[0].get('model') or better_if_installed[0].get('capability')} would score higher in {mode} mode."}
               if better_if_installed else {}),
        })
    return {
        "task": task,
        "mode": mode,
        "claude": pick_model(task, mode, files_involved, current_model),
        "recommendations": parts,
        "limits": "CIE cannot switch the model of the conversation calling it. Claude applies the model and effort by "
                  "running the subtask on a helper with those settings, where the app offers helpers. Claude models run "
                  "on Anthropic's servers. No paid API key is needed.",
    }


def status() -> dict[str, Any]:
    from .models import list_models

    return {
        "mode": current_mode(),
        "modes": list(MODES),
        "local_models": [{"id": m["id"], "task": m["task"], "installed": m["installed"], "size_mb": m["size_mb"]}
                         for m in list_models()["models"]],
        "categories": list(CATEGORIES),
        "claude_models": list(MODEL_TIERS),
        "efforts": list(EFFORTS),
        "model_table": {mode: {k: {"model": m, "effort": e} for k, (m, e) in table.items()} for mode, table in MODEL_TABLE.items()},
    }


def register_capabilities(reg: Any) -> None:
    from .registry import CapabilitySpec

    def wrap(fn: Any, summary: str) -> Any:
        def handler(args: dict[str, Any]) -> dict[str, Any]:
            try:
                return {"status": "ok", "summary": summary, "data": fn(args)}
            except ValueError as exc:
                return {"status": "error", "summary": str(exc)}
        return handler

    reg.register(CapabilitySpec(
        id="routing.recommend",
        area="routing",
        summary="Pick how to do a task: a free built-in CIE method, an installed local model, or a Claude model + effort level, and whether to run it on a helper. Call before substantial work.",
        input_schema={"task": "str", "mode": "optional str", "files_involved": "optional int", "current_model": "optional str"},
        permission="read",
        handler=wrap(lambda a: recommend(a["task"], a.get("mode", ""), int(a.get("files_involved", 0)), a.get("current_model", "")), "Routing recommendation ready."),
        tags=["routing", "model", "effort", "subagent", "helper", "tokens", "privacy"],
    ))
    reg.register(CapabilitySpec(
        id="routing.status",
        area="routing",
        summary="Show the routing mode and which optional local models are installed.",
        input_schema={},
        permission="read",
        handler=wrap(lambda a: status(), "Routing status."),
        tags=["routing", "model"],
    ))
    reg.register(CapabilitySpec(
        id="routing.set_mode",
        area="routing",
        summary="Change the default routing mode (CIE config only).",
        input_schema={"mode": "str"},
        permission="write",
        handler=wrap(lambda a: set_mode(a["mode"]), "Routing mode set."),
        tags=["routing", "settings"],
    ))
