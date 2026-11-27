"""Phase 1.5 verified template and component library.

Built-in templates ship inside CIE (so they need no setup); user templates
live in CIE_HOME/templates. Every template carries metadata, compatibility,
version, provenance, and a validation status that CIE computes by actually
checking the files (JSON parses, Python compiles, OBJ has faces, PNG opens,
Hearth hookup rules hold, every {{placeholder}} is filled).

Applying a template never overwrites a file, and takes a snapshot that
records the new files as "did not exist", so storage.restore removes them.
"""

from __future__ import annotations

import ast
import io
import json
import re
from pathlib import Path
from typing import Any

from .config import get_cie_home, resolve_in_projects, utc_now

PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
KINDS = ("code", "ui", "2d", "3d", "material", "animation", "project", "workflow")
BUILTIN_PROVENANCE = {"author": "CIE built-in", "license": "MIT", "source": "cie/templates.py"}

# --------------------------------------------------------------------------
# Built-in templates
# --------------------------------------------------------------------------

HEARTH_INDEX = """<!DOCTYPE html>
<html lang="en">
<head>
  <script src="../../sdk/hearth-connect.js"></script>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{title}}</title>
  <style>
    html, body { margin: 0; height: 100%; background: #0b0f17; color: #e6edf3; font-family: system-ui, sans-serif; }
    canvas { display: block; width: 100vw; height: 100vh; image-rendering: pixelated; }
    #hud { position: fixed; top: 12px; left: 12px; font-size: 14px; opacity: .85; }
  </style>
</head>
<body>
  <canvas id="game"></canvas>
  <div id="hud">Score: <span id="score">0</span></div>
  <script src="game.js"></script>
</body>
</html>
"""

HEARTH_GAME_JS = """// {{title}} - Hearth-ready browser game skeleton.
// Saves use plain localStorage only: inside Hearth it becomes the player's save slot.
'use strict';

const SAVE_KEY = '{{game_id}}-save';
const canvas = document.getElementById('game');
const ctx = canvas.getContext('2d');
const scoreEl = document.getElementById('score');

function unlock(id) {
  if (window.Hearth) Hearth.unlock(id);
}

function loadSave() {
  try { return JSON.parse(localStorage.getItem(SAVE_KEY)) || { best: 0, plays: 0 }; }
  catch { return { best: 0, plays: 0 }; }
}

function writeSave(save) {
  localStorage.setItem(SAVE_KEY, JSON.stringify(save));
}

const save = loadSave();
save.plays += 1;
writeSave(save);
unlock('first-steps');

const state = { score: 0, last: performance.now() };

function resize() {
  canvas.width = Math.floor(window.innerWidth / 2);
  canvas.height = Math.floor(window.innerHeight / 2);
}
window.addEventListener('resize', resize);
resize();

function update(dt) {
  state.score += dt;
  if (state.score >= 60) unlock('one-minute');
}

function draw() {
  ctx.fillStyle = '#0b0f17';
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = '#58a6ff';
  const s = 16;
  ctx.fillRect(canvas.width / 2 - s / 2, canvas.height / 2 - s / 2, s, s);
  scoreEl.textContent = Math.floor(state.score);
}

function frame(now) {
  const dt = Math.min(0.05, (now - state.last) / 1000);
  state.last = now;
  update(dt);
  draw();
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

window.addEventListener('beforeunload', () => {
  save.best = Math.max(save.best, Math.floor(state.score));
  writeSave(save);
});
"""

HEARTH_ENTRY = """// Merge this entry into Hearth's data/games.js.
// Achievement ids must match the Hearth.unlock("id") calls in game.js.
{
  id: "{{game_id}}",
  title: "{{title}}",
  status: "playable",
  path: "games/{{game_id}}/index.html",
  achievements: [
    { id: "first-steps", name: "First Steps", description: "Start {{title}} for the first time.", hidden: false },
    { id: "one-minute", name: "Survivor", description: "Last one minute.", hidden: false }
  ]
}
"""

LUAU_MODULE = """--!strict
-- {{module_name}}: ModuleScript. Require from server or client scripts.
local {{module_name}} = {}
{{module_name}}.__index = {{module_name}}

export type {{module_name}} = typeof(setmetatable({} :: {
\tname: string,
}, {{module_name}}))

function {{module_name}}.new(name: string): {{module_name}}
\tlocal self = setmetatable({}, {{module_name}})
\tself.name = name
\treturn self
end

function {{module_name}}:Describe(): string
\treturn ("{{module_name}}(%s)"):format(self.name)
end

return {{module_name}}
"""

LUAU_REMOTES_SERVER = """--!strict
-- Server side of a validated RemoteEvent. Never trust values from the client.
local ReplicatedStorage = game:GetService("ReplicatedStorage")

local remote = Instance.new("RemoteEvent")
remote.Name = "{{event_name}}"
remote.Parent = ReplicatedStorage

local lastCall: { [Player]: number } = {}
local MIN_INTERVAL = 0.25 -- seconds between calls per player

remote.OnServerEvent:Connect(function(player: Player, payload: any)
\tlocal now = os.clock()
\tif lastCall[player] and now - lastCall[player] < MIN_INTERVAL then
\t\treturn -- rate limited
\tend
\tlastCall[player] = now
\tif typeof(payload) ~= "table" or typeof(payload.action) ~= "string" then
\t\treturn -- reject malformed input
\tend
\t-- TODO: handle payload.action on the server
end)

game:GetService("Players").PlayerRemoving:Connect(function(player: Player)
\tlastCall[player] = nil
end)
"""

LUAU_REMOTES_CLIENT = """--!strict
-- Client side: fire the {{event_name}} RemoteEvent with a small table payload.
local ReplicatedStorage = game:GetService("ReplicatedStorage")
local remote = ReplicatedStorage:WaitForChild("{{event_name}}") :: RemoteEvent

local function send(action: string)
\tremote:FireServer({ action = action })
end

return send
"""

GODOT_PLAYER = """extends CharacterBody2D
## {{class_name}}: 8-direction top-down movement with acceleration.

@export var max_speed: float = 180.0
@export var acceleration: float = 1200.0
@export var friction: float = 1400.0

func _physics_process(delta: float) -> void:
\tvar input := Input.get_vector("ui_left", "ui_right", "ui_up", "ui_down")
\tif input != Vector2.ZERO:
\t\tvelocity = velocity.move_toward(input * max_speed, acceleration * delta)
\telse:
\t\tvelocity = velocity.move_toward(Vector2.ZERO, friction * delta)
\tmove_and_slide()
"""

PAUSE_MENU = """<!-- {{title}} pause menu component. Toggle with Escape or togglePause(). -->
<div id="pause-menu" role="dialog" aria-modal="true" aria-label="Paused" hidden>
  <div class="pm-panel">
    <h2>Paused</h2>
    <button data-action="resume">Resume</button>
    <button data-action="settings">Settings</button>
    <button data-action="quit">Quit to menu</button>
  </div>
</div>
<style>
  #pause-menu { position: fixed; inset: 0; display: grid; place-items: center; background: rgba(5,8,14,.72); }
  #pause-menu[hidden] { display: none; }
  .pm-panel { min-width: 240px; padding: 24px; border-radius: 12px; background: #161b22; color: #e6edf3; display: grid; gap: 10px; }
  .pm-panel h2 { margin: 0 0 6px; font: 600 20px system-ui, sans-serif; }
  .pm-panel button { padding: 10px 14px; border: 0; border-radius: 8px; background: #21262d; color: inherit; font: 15px system-ui; cursor: pointer; }
  .pm-panel button:hover, .pm-panel button:focus-visible { background: #30363d; outline: 2px solid #58a6ff; }
</style>
<script>
  (function () {
    const menu = document.getElementById('pause-menu');
    window.togglePause = function (force) {
      const open = typeof force === 'boolean' ? force : menu.hidden;
      menu.hidden = !open;
      if (open) menu.querySelector('button').focus();
      document.dispatchEvent(new CustomEvent(open ? 'game:pause' : 'game:resume'));
    };
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') window.togglePause(); });
    menu.addEventListener('click', (e) => {
      const action = e.target.dataset && e.target.dataset.action;
      if (action === 'resume') window.togglePause(false);
      if (action) document.dispatchEvent(new CustomEvent('pause:' + action));
    });
  })();
</script>
"""

UV_CUBE_OBJ = """# {{name}}: unit cube with per-face UVs (0..1), 8 vertices, 6 quads
mtllib {{name}}.mtl
o {{name}}
v -0.5 -0.5  0.5
v  0.5 -0.5  0.5
v  0.5  0.5  0.5
v -0.5  0.5  0.5
v -0.5 -0.5 -0.5
v  0.5 -0.5 -0.5
v  0.5  0.5 -0.5
v -0.5  0.5 -0.5
vt 0 0
vt 1 0
vt 1 1
vt 0 1
vn 0 0 1
vn 0 0 -1
vn 1 0 0
vn -1 0 0
vn 0 1 0
vn 0 -1 0
usemtl {{name}}_mat
f 1/1/1 2/2/1 3/3/1 4/4/1
f 6/1/2 5/2/2 8/3/2 7/4/2
f 2/1/3 6/2/3 7/3/3 3/4/3
f 5/1/4 1/2/4 4/3/4 8/4/4
f 4/1/5 3/2/5 7/3/5 8/4/5
f 5/1/6 6/2/6 2/3/6 1/4/6
"""

UV_CUBE_MTL = """newmtl {{name}}_mat
Kd 0.8 0.8 0.8
Ka 0 0 0
Ks 0.1 0.1 0.1
Ns 32
d 1
"""

PBR_MATERIAL = """{
  "name": "{{name}}",
  "pbrMetallicRoughness": {
    "baseColorFactor": [{{r}}, {{g}}, {{b}}, 1.0],
    "metallicFactor": {{metallic}},
    "roughnessFactor": {{roughness}}
  },
  "doubleSided": false,
  "alphaMode": "OPAQUE"
}
"""

EASING_PRESETS = """{
  "name": "{{name}}",
  "fps": 30,
  "note": "Cubic-bezier control points (x1, y1, x2, y2); same convention as CSS.",
  "presets": {
    "linear":      [0.0, 0.0, 1.0, 1.0],
    "ease_in":     [0.42, 0.0, 1.0, 1.0],
    "ease_out":    [0.0, 0.0, 0.58, 1.0],
    "ease_in_out": [0.42, 0.0, 0.58, 1.0],
    "snappy":      [0.2, 0.9, 0.3, 1.0],
    "anticipate":  [0.36, -0.4, 0.6, 1.0],
    "overshoot":   [0.34, 1.56, 0.64, 1.0]
  },
  "timing_guide_frames": { "ui_fade": 6, "button_press": 3, "jump_squash": 4, "hit_flash": 2, "idle_loop": 24 }
}
"""

ASSET_AUDIT_WORKFLOW = """{
  "name": "Asset audit for {{project_id}}",
  "how_to_run": "cie_run('orchestrate.run', {'steps': <steps below>})",
  "steps": [
    {"id": "summary", "capability_id": "knowledge.summary", "args": {"project_id": "{{project_id}}"}},
    {"id": "npot", "capability_id": "knowledge.assets", "args": {"project_id": "{{project_id}}", "kind": "image", "where": {"power_of_two": false}}},
    {"id": "dupes", "capability_id": "storage.duplicates", "args": {"project_id": "{{project_id}}"}},
    {"id": "space", "capability_id": "storage.report", "args": {}}
  ]
}
"""

BROWSER_PROJECT_README = """# {{title}}

Browser game project layout created by CIE.

- `src/` game code (`main.js` entry)
- `assets/sprites`, `assets/audio`, `assets/fonts` source assets
- `docs/design.md` design notes and the phase plan
- `build/` generated files (ignored by CIE indexing)
"""

BUILTINS: dict[str, dict[str, Any]] = {
    "hearth-browser-game": {
        "name": "Hearth-ready browser game", "kind": "project", "version": "1.0.0",
        "description": "Canvas game skeleton wired for Hearth: hearth-connect.js first in <head>, localStorage saves, guarded Hearth.unlock achievements, and a data/games.js entry.",
        "compatibility": {"engines": ["browser", "hearth"], "languages": ["javascript", "html"]},
        "tags": ["hearth", "browser", "canvas", "achievements", "starter"],
        "parameters": {"game_id": {"required": True}, "title": {"required": True}},
        "files": {
            "games/{{game_id}}/index.html": HEARTH_INDEX,
            "games/{{game_id}}/game.js": HEARTH_GAME_JS,
            "games/{{game_id}}/hearth-entry.js": HEARTH_ENTRY,
        },
        "checks": ["hearth"],
    },
    "luau-module": {
        "name": "Luau strict ModuleScript", "kind": "code", "version": "1.0.0",
        "description": "Typed class-style ModuleScript with --!strict and an exported type.",
        "compatibility": {"engines": ["roblox"], "languages": ["luau"]},
        "tags": ["roblox", "luau", "module"],
        "parameters": {"module_name": {"required": True}},
        "files": {"{{module_name}}.luau": LUAU_MODULE},
    },
    "luau-validated-remote": {
        "name": "Validated RemoteEvent (server + client)", "kind": "code", "version": "1.0.0",
        "description": "Server RemoteEvent with per-player rate limiting and payload validation, plus a client sender.",
        "compatibility": {"engines": ["roblox"], "languages": ["luau"]},
        "tags": ["roblox", "luau", "networking", "security"],
        "parameters": {"event_name": {"required": True}},
        "files": {
            "server/{{event_name}}.server.luau": LUAU_REMOTES_SERVER,
            "client/{{event_name}}.client.luau": LUAU_REMOTES_CLIENT,
        },
    },
    "godot-topdown-player": {
        "name": "Godot 4 top-down player", "kind": "code", "version": "1.0.0",
        "description": "CharacterBody2D with 8-direction movement, acceleration, and friction.",
        "compatibility": {"engines": ["godot>=4.0"], "languages": ["gdscript"]},
        "tags": ["godot", "gdscript", "movement", "2d"],
        "parameters": {"class_name": {"required": False, "default": "Player"}},
        "files": {"{{class_name}}.gd": GODOT_PLAYER},
    },
    "html-pause-menu": {
        "name": "Accessible pause menu", "kind": "ui", "version": "1.0.0",
        "description": "Escape-toggled pause dialog with focus handling and game:pause/resume events.",
        "compatibility": {"engines": ["browser"], "languages": ["html", "css", "javascript"]},
        "tags": ["ui", "menu", "browser", "accessibility"],
        "parameters": {"title": {"required": False, "default": "Game"}},
        "files": {"pause-menu.html": PAUSE_MENU},
    },
    "tileset-guide": {
        "name": "Tileset guide sheet", "kind": "2d", "version": "1.0.0",
        "description": "Transparent PNG grid with numbered cells for drawing a tileset at a fixed tile size.",
        "compatibility": {"engines": ["any"], "formats": ["png"]},
        "tags": ["2d", "tileset", "pixel-art", "guide"],
        "parameters": {"name": {"required": False, "default": "tileset_guide"},
                       "tile": {"required": False, "default": "16"}, "cols": {"required": False, "default": "8"},
                       "rows": {"required": False, "default": "8"}},
        "files": {"{{name}}.png": {"generator": "tileset_guide"}},
    },
    "uv-cube": {
        "name": "UV-mapped cube (OBJ + MTL)", "kind": "3d", "version": "1.0.0",
        "description": "Unit cube with per-face UVs and normals, ready for texturing tests.",
        "compatibility": {"engines": ["any"], "formats": ["obj", "mtl"]},
        "tags": ["3d", "mesh", "uv", "primitive"],
        "parameters": {"name": {"required": False, "default": "cube"}},
        "files": {"{{name}}.obj": UV_CUBE_OBJ, "{{name}}.mtl": UV_CUBE_MTL},
    },
    "pbr-material": {
        "name": "glTF PBR material", "kind": "material", "version": "1.0.0",
        "description": "glTF 2.0 metallic-roughness material definition.",
        "compatibility": {"engines": ["gltf", "godot", "three.js", "blender"], "formats": ["json"]},
        "tags": ["material", "pbr", "gltf"],
        "parameters": {"name": {"required": False, "default": "material"}, "r": {"required": False, "default": "0.8"},
                       "g": {"required": False, "default": "0.8"}, "b": {"required": False, "default": "0.8"},
                       "metallic": {"required": False, "default": "0.0"}, "roughness": {"required": False, "default": "0.6"}},
        "files": {"{{name}}.material.json": PBR_MATERIAL},
    },
    "easing-presets": {
        "name": "Animation easing presets", "kind": "animation", "version": "1.0.0",
        "description": "Cubic-bezier easing presets and frame-timing guide at 30 FPS.",
        "compatibility": {"engines": ["any"], "formats": ["json"]},
        "tags": ["animation", "easing", "timing"],
        "parameters": {"name": {"required": False, "default": "easing"}},
        "files": {"{{name}}.easing.json": EASING_PRESETS},
    },
    "asset-audit-workflow": {
        "name": "Asset audit workflow", "kind": "workflow", "version": "1.0.0",
        "description": "orchestrate.run steps: project summary, non-power-of-two images, duplicates, disk use.",
        "compatibility": {"engines": ["cie"], "formats": ["json"]},
        "tags": ["workflow", "assets", "audit"],
        "parameters": {"project_id": {"required": True}},
        "files": {"asset-audit.workflow.json": ASSET_AUDIT_WORKFLOW},
    },
    "browser-game-layout": {
        "name": "Browser game project layout", "kind": "project", "version": "1.0.0",
        "description": "Folder structure for a standalone browser game (src, assets, docs, build).",
        "compatibility": {"engines": ["browser"], "languages": ["javascript"]},
        "tags": ["project", "structure", "browser"],
        "parameters": {"title": {"required": True}},
        "files": {
            "README.md": BROWSER_PROJECT_README,
            "src/main.js": "// {{title}} entry point\n'use strict';\n",
            "assets/sprites/.keep": "",
            "assets/audio/.keep": "",
            "assets/fonts/.keep": "",
            "docs/design.md": "# {{title}} design\n\n## Phases\n\n1. Core loop\n2. Content\n3. Polish\n",
        },
    },
}


# --------------------------------------------------------------------------
# Generators for binary template files
# --------------------------------------------------------------------------

def _gen_tileset_guide(params: dict[str, str]) -> bytes:
    from PIL import Image, ImageDraw

    tile = max(4, min(int(params.get("tile", "16")), 256))
    cols = max(1, min(int(params.get("cols", "8")), 64))
    rows = max(1, min(int(params.get("rows", "8")), 64))
    img = Image.new("RGBA", (tile * cols, tile * rows), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for c in range(cols):
        for r in range(rows):
            x0, y0 = c * tile, r * tile
            shade = (255, 0, 255, 40) if (c + r) % 2 else (0, 255, 255, 40)
            d.rectangle([x0, y0, x0 + tile - 1, y0 + tile - 1], fill=shade, outline=(255, 255, 255, 110))
            if tile >= 12:
                d.text((x0 + 2, y0 + 1), str(r * cols + c), fill=(255, 255, 255, 160))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


GENERATORS = {"tileset_guide": _gen_tileset_guide}


# --------------------------------------------------------------------------
# Library access
# --------------------------------------------------------------------------

def _user_root() -> Path:
    root = get_cie_home() / "templates"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _load_user(tid: str) -> dict[str, Any] | None:
    meta_path = _user_root() / tid / "template.json"
    if not meta_path.is_file():
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    files_dir = _user_root() / tid / "files"
    meta["files"] = {p.relative_to(files_dir).as_posix(): p.read_text(encoding="utf-8")
                     for p in files_dir.rglob("*") if p.is_file()}
    return meta


def get_template(tid: str) -> dict[str, Any]:
    if tid in BUILTINS:
        return {"id": tid, **BUILTINS[tid], "provenance": BUILTIN_PROVENANCE, "origin": "builtin"}
    user = _load_user(tid)
    if user:
        return {"id": tid, **user, "origin": "user"}
    raise ValueError(f"No template with id '{tid}'. Use templates.list.")


def _fill(text: str, params: dict[str, str]) -> str:
    return PLACEHOLDER_RE.sub(lambda m: params.get(m.group(1), m.group(0)), text)


def _resolve_params(tpl: dict[str, Any], given: dict[str, Any] | None) -> dict[str, str]:
    given = {k: str(v) for k, v in (given or {}).items()}
    out: dict[str, str] = {}
    missing = []
    for name, spec in (tpl.get("parameters") or {}).items():
        if name in given:
            out[name] = given[name]
        elif "default" in spec:
            out[name] = str(spec["default"])
        elif spec.get("required"):
            missing.append(name)
    if missing:
        raise ValueError(f"Missing template parameter(s): {', '.join(missing)}")
    for name, value in out.items():
        if name in {"game_id", "module_name", "event_name", "class_name", "name", "project_id"}:
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value):
                raise ValueError(f"Parameter '{name}' must be letters, digits, '_', '-' or '.' (max 64).")
    return out


def render(tid: str, params: dict[str, Any] | None = None) -> dict[str, bytes]:
    tpl = get_template(tid)
    values = _resolve_params(tpl, params)
    rendered: dict[str, bytes] = {}
    for rel, content in tpl["files"].items():
        path = _fill(rel, values)
        if isinstance(content, dict) and "generator" in content:
            rendered[path] = GENERATORS[content["generator"]](values)
        else:
            rendered[path] = _fill(content, values).encode("utf-8")
    return rendered


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def _check_file(path: str, data: bytes, checks: list[str]) -> list[str]:
    problems: list[str] = []
    ext = Path(path).suffix.lower()
    text = data.decode("utf-8", "replace") if ext not in {".png"} else ""
    if text and PLACEHOLDER_RE.search(text):
        problems.append(f"{path}: unfilled placeholder {PLACEHOLDER_RE.search(text).group(0)}")
    try:
        if ext == ".json":
            json.loads(text)
        elif ext == ".py":
            ast.parse(text)
        elif ext == ".png":
            from PIL import Image
            with Image.open(io.BytesIO(data)) as im:
                im.verify()
        elif ext == ".obj":
            verts = sum(1 for ln in text.splitlines() if ln.startswith("v "))
            faces = [ln for ln in text.splitlines() if ln.startswith("f ")]
            if not verts or not faces:
                problems.append(f"{path}: OBJ needs vertices and faces")
            for f in faces:
                for ref in f.split()[1:]:
                    idx = int(ref.split("/")[0])
                    if idx < 1 or idx > verts:
                        problems.append(f"{path}: face index {idx} out of range")
                        break
        elif ext in {".luau", ".lua", ".gd", ".js"}:
            pairs = {"(": ")", "[": "]", "{": "}"}
            stack: list[str] = []
            for ch in re.sub(r"(\"(\\.|[^\"])*\"|'(\\.|[^'])*'|--[^\n]*|//[^\n]*)", "", text):
                if ch in pairs:
                    stack.append(pairs[ch])
                elif ch in pairs.values():
                    if not stack or stack.pop() != ch:
                        problems.append(f"{path}: unbalanced brackets")
                        break
            if stack and not problems:
                problems.append(f"{path}: unclosed brackets")
            if ext == ".luau" and not text.lstrip().startswith("--!strict"):
                problems.append(f"{path}: Luau templates must start with --!strict")
    except Exception as exc:  # noqa: BLE001 - report as a validation failure
        problems.append(f"{path}: {type(exc).__name__}: {exc}")
    return problems


def _hearth_checks(files: dict[str, bytes]) -> list[str]:
    problems = []
    for path, data in files.items():
        text = data.decode("utf-8", "replace")
        if path.endswith("index.html"):
            head = text.split("<head>", 1)[-1].split("</head>", 1)[0]
            scripts = re.findall(r"<script[^>]*src=\"([^\"]+)\"", head)
            first_tag = re.search(r"<(script|meta|title|style|link)\b[^>]*>", head)
            if not scripts or "hearth-connect.js" not in scripts[0] or not first_tag or first_tag.group(1) != "script":
                problems.append(f"{path}: hearth-connect.js must be the first element in <head>")
        if path.endswith(".js") and "hearth-entry" not in path:
            if re.search(r"indexedDB|document\.cookie", text):
                problems.append(f"{path}: Hearth games save with localStorage only")
            for m in re.finditer(r"Hearth\.unlock\(", text):
                line = text[:m.start()].rsplit("\n", 1)[-1]
                if "window.Hearth" not in line:
                    problems.append(f"{path}: Hearth.unlock must be guarded by if (window.Hearth)")
                    break
        if path.endswith("hearth-entry.js"):
            ids = set(re.findall(r"id:\s*\"([^\"]+)\"", text))
            js = next((d.decode("utf-8", "replace") for p, d in files.items() if p.endswith("game.js")), "")
            used = set(re.findall(r"unlock\('([^']+)'\)", js)) | set(re.findall(r"unlock\(\"([^\"]+)\"\)", js))
            if used - ids:
                problems.append(f"{path}: achievements used in game.js but not listed: {sorted(used - ids)}")
    return problems


def validate(tid: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    tpl = get_template(tid)
    sample = {name: (spec.get("default") or f"Sample_{name}") for name, spec in (tpl.get("parameters") or {}).items()}
    sample.update({k: str(v) for k, v in (params or {}).items()})
    files = render(tid, sample)
    problems: list[str] = []
    for path, data in files.items():
        problems += _check_file(path, data, tpl.get("checks") or [])
    if "hearth" in (tpl.get("checks") or []):
        problems += _hearth_checks(files)
    status = "passed" if not problems else "failed"
    result = {"id": tid, "status": status, "files_checked": len(files), "problems": problems, "validated_at": utc_now()}
    if tpl["origin"] == "user":
        meta_path = _user_root() / tid / "template.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["validation"] = {k: result[k] for k in ("status", "problems", "validated_at")}
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return result


# --------------------------------------------------------------------------
# Capabilities' operations
# --------------------------------------------------------------------------

def list_templates(kind: str = "", tag: str = "", engine: str = "") -> dict[str, Any]:
    items = []
    ids = list(BUILTINS) + sorted(p.name for p in _user_root().iterdir() if (p / "template.json").is_file())
    for tid in ids:
        t = get_template(tid)
        if kind and t["kind"] != kind:
            continue
        if tag and tag not in t.get("tags", []):
            continue
        if engine and not any(engine.lower() in e.lower() for e in (t.get("compatibility") or {}).get("engines", [])):
            continue
        items.append({
            "id": tid, "name": t["name"], "kind": t["kind"], "version": t["version"], "origin": t["origin"],
            "description": t["description"], "tags": t.get("tags", []), "compatibility": t.get("compatibility", {}),
            "parameters": {k: ("required" if v.get("required") else f"default {v.get('default')!r}")
                           for k, v in (t.get("parameters") or {}).items()},
            "validation": t.get("validation", {}).get("status") if t["origin"] == "user" else "checked by tests",
        })
    return {"templates": items, "count": len(items)}


def show(tid: str) -> dict[str, Any]:
    t = get_template(tid)
    files = {}
    for rel, content in t["files"].items():
        files[rel] = (f"<generated by {content['generator']}>" if isinstance(content, dict)
                      else content[:1500] + ("…" if len(content) > 1500 else ""))
    return {k: v for k, v in t.items() if k != "files"} | {"files": files}


def apply(tid: str, target_dir: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    from .snapstore import create_snapshot

    target, root = resolve_in_projects(target_dir)
    if target.exists() and not target.is_dir():
        raise ValueError("target_dir must be a folder.")
    files = render(tid, params)
    planned = {(target / rel).resolve(): data for rel, data in files.items()}
    for path in planned:
        if target.resolve() not in path.parents:
            raise ValueError(f"Template path escapes the target folder: {path}")
    conflicts = [str(p) for p in planned if p.exists()]
    if conflicts:
        raise ValueError(f"These files already exist, so nothing was written: {conflicts}")
    report = validate(tid, params)
    if report["status"] != "passed":
        raise ValueError(f"Template failed validation, so nothing was written: {report['problems']}")
    snap = create_snapshot(paths=[str(p) for p in planned], kind="auto", description=f"Before applying template {tid}")
    for path, data in planned.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return {"template": tid, "created": [str(p) for p in planned], "project_id": root["id"],
            "snapshot_id": snap["snapshot_id"],
            "undo": f"storage.restore with snapshot_id {snap['snapshot_id']} removes these files."}


def save_user_template(tid: str, name: str, kind: str, description: str, paths: list[str],
                       base_dir: str, tags: list[str] | None = None, parameters: dict[str, Any] | None = None,
                       version: str = "1.0.0", engines: list[str] | None = None) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,63}", tid):
        raise ValueError("Template id must be lowercase letters, digits, and '-' (2-64 chars).")
    if tid in BUILTINS:
        raise ValueError("That id belongs to a built-in template.")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of: {', '.join(KINDS)}")
    base, _ = resolve_in_projects(base_dir)
    dest = _user_root() / tid
    if dest.exists():
        raise ValueError(f"User template '{tid}' already exists.")
    files: dict[str, str] = {}
    for raw in paths:
        p, _ = resolve_in_projects(raw)
        if base.resolve() not in p.resolve().parents:
            raise ValueError(f"{p} is not inside base_dir.")
        try:
            files[p.relative_to(base).as_posix()] = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise ValueError(f"{p} is not a text file; user templates hold text files only.") from None
    (dest / "files").mkdir(parents=True)
    for rel, text in files.items():
        out = dest / "files" / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    meta = {
        "name": name, "kind": kind, "version": version, "description": description,
        "tags": tags or [], "compatibility": {"engines": engines or []},
        "parameters": parameters or {},
        "provenance": {"author": "user", "saved_from": str(base), "saved_at": utc_now()},
    }
    (dest / "template.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    result = validate(tid)
    return {"id": tid, "files": sorted(files), "validation": result["status"], "problems": result["problems"]}


def register_capabilities(reg: Any) -> None:
    from .registry import CapabilitySpec

    def wrap(fn: Any, summary: str) -> Any:
        def handler(args: dict[str, Any]) -> dict[str, Any]:
            try:
                return {"status": "ok", "summary": summary, "data": fn(args)}
            except (ValueError, PermissionError) as exc:
                return {"status": "error", "summary": str(exc)}
        return handler

    reg.register(CapabilitySpec(
        id="templates.list", area="templates",
        summary="List verified templates (Hearth browser game, Luau modules, Roblox remotes, Godot player, UI, tilesets, 3D, materials, animation, workflows).",
        input_schema={"kind": "optional str", "tag": "optional str", "engine": "optional str"},
        permission="read", handler=wrap(lambda a: list_templates(a.get("kind", ""), a.get("tag", ""), a.get("engine", "")), "Templates listed."),
        tags=["templates", "starter", "library"],
    ))
    reg.register(CapabilitySpec(
        id="templates.show", area="templates",
        summary="Show a template's metadata, provenance, parameters, and file contents.",
        input_schema={"id": "str"},
        permission="read", handler=wrap(lambda a: show(a["id"]), "Template shown."),
        tags=["templates"],
    ))
    reg.register(CapabilitySpec(
        id="templates.validate", area="templates",
        summary="Check a template's files for real problems (parsing, OBJ faces, PNG decode, Hearth rules, unfilled placeholders).",
        input_schema={"id": "str", "params": "optional object"},
        permission="read", handler=wrap(lambda a: validate(a["id"], a.get("params")), "Template validated."),
        tags=["templates", "validation"],
    ))
    reg.register(CapabilitySpec(
        id="templates.apply", area="templates",
        summary="Create a template's files in a project folder. Never overwrites; validates first; snapshot makes it undoable.",
        input_schema={"id": "str", "target_dir": "str", "params": "optional object"},
        permission="write", handler=wrap(lambda a: apply(a["id"], a["target_dir"], a.get("params")), "Template applied."),
        tags=["templates", "create"],
    ))
    reg.register(CapabilitySpec(
        id="templates.save", area="templates",
        summary="Save project text files as a reusable user template with metadata, provenance, and validation.",
        input_schema={"id": "str", "name": "str", "kind": "str", "description": "str", "paths": "list[str]",
                      "base_dir": "str", "tags": "optional list[str]", "parameters": "optional object",
                      "version": "optional str", "engines": "optional list[str]"},
        permission="write",
        handler=wrap(lambda a: save_user_template(a["id"], a["name"], a["kind"], a["description"], a["paths"], a["base_dir"],
                                                  a.get("tags"), a.get("parameters"), a.get("version", "1.0.0"), a.get("engines")),
                     "Template saved."),
        tags=["templates", "save"],
    ))
