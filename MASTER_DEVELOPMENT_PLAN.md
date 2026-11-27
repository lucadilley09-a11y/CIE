# Creative Intelligence Engine (CIE) — Master Development Plan

**Plan baseline:** CIE v2.3.2  
**Status:** approved product direction; Phase 0 implementation remains in progress  
**Primary interface:** Claude through MCP  
**Secondary interfaces:** local CLI and dashboard

## 1. Product objective

CIE is a free, local-first capability layer that makes an AI assistant substantially more effective at software development and creative production. It supplies reliable tools, project context, persistent artifact storage, execution, validation, orchestration, and evidence. It does not claim to change the underlying model weights or inherent intelligence of Claude or any other model.

The normal workflow should be highly automatic and require minimal user coordination and minimal model tokens. CIE should perform routine retrieval, planning, safe execution, validation, caching, and concise reporting internally. It should expose a small, high-value MCP tool surface rather than forcing the model to call many low-level tools.

## 2. Non-negotiable product constraints

- **100% free core:** no paid APIs, subscriptions, credits, or mandatory cloud services.
- **Near-zero setup:** no required external application, local model, account, or API key.
- **Online-first installation:** first-time setup and updates may download pinned dependencies. Normal runtime workflows remain local-first; no hosted CIE service or paid API is required. Cached/offline installation is optional, not a release requirement.
- **Tier 1 is complete by itself:** CIE-owned implementations plus approved free libraries provide real usable functionality; no essential feature is merely a placeholder.
- **Tier 2 is optional:** user-selected local AI models may improve selected tasks but are never required.
- **Tier 3 is optional:** Blender, Godot, Unity, Unreal, ComfyUI, and other external applications may enhance workflows but are never prerequisites for Tier 1.
- **Claude-first MCP:** optimize the primary interaction for Claude, while keeping CLI/dashboard access.
- **Permission safety:** safe and reversible operations may be automated; destructive, external, privacy-sensitive, or consequential actions require the established permission policy.
- **Honest evidence:** never report a test, build, model, application, or platform as available or successful without evidence.
- **Token efficiency:** use local processing, selective retrieval, compact structured results, caching, deduplication, and bounded output.

## 3. Architecture

### 3.1 Core runtime

- Small MCP interface with high-level task-oriented tools.
- Internal workflow orchestrator decomposes requests into bounded steps and selects capabilities.
- Typed schemas validate all inputs and outputs.
- Async job executor manages timeouts, cancellation, resource limits, and job lifecycle.
- Permission engine classifies risk and gates protected actions.
- Server-side token/context governor selects only relevant context and enforces output budgets.
- Anchored audit trail records operations, inputs, outputs, approvals, and artifact changes.
- Local capability registry records tool availability, versions, requirements, risk, and validation status.

### 3.2 Persistent project, code, and asset storage

CIE must preserve actual artifacts, not just conversational summaries:

- Source code: files, modules, classes, functions, patches, project structure, and generated implementations.
- 2D: sprites, sprite sheets, animation frames, textures, palettes, UI assets, layered/editable source, and exports.
- 3D: meshes, materials, textures, UVs, rigs, skeletons, animations, source files, and exports.
- Audio: generated/processed files, presets, stems where available, loop metadata, and manifests.
- Project knowledge: architecture, decisions, constraints, bugs, dependency facts, and verified build/test results.

Storage design:
- Filesystem storage for the actual bytes and editable source artifacts.
- SQLite metadata database for identities, projects, versions, dependencies, tags, relationships, provenance, and validation status.
- SHA-256 content-addressed storage to deduplicate identical content.
- FTS5, symbol graph, metadata, and asset-specific indexes for retrieval.
- Version history with immutable prior versions and clear current/approved pointers.
- Distinct temporary, draft, verified, and approved/reusable asset states.
- Automatic linking between related assets (for example model, material, texture, rig, and animation).
- Compatibility and quality checks before reuse; stale or incompatible assets must not be reused blindly.
- Configurable automatic saving with user-controlled exclusions and project boundaries.
- Persistent artifact storage remains distinct from conversational memory, but both may be queried together with provenance.

#### 3.2.1 Storage efficiency and lifecycle manager

Storage efficiency is a first-class product requirement. CIE should minimize disk usage without silently reducing asset quality, deleting user work, or weakening version history and rollback. The storage manager must operate automatically within explicit, user-configurable safety limits.

- **Content-level deduplication:** hash files and, where useful, large binary chunks; store identical content once and reference it from multiple projects. Preserve project-level paths and metadata so deduplication is invisible to normal workflows.
- **Format-aware compression:** use lossless compression by default for source code, project metadata, editable originals, and data where exact fidelity matters. For textures, meshes, audio, and exports, select efficient formats appropriate to the target engine and quality requirements. Lossy compression, texture downscaling, mesh simplification, and audio bitrate reduction require a declared quality budget and must preserve the editable source.
- **Derived-artifact separation:** distinguish authoritative source assets from reproducible exports, previews, thumbnails, intermediate renders, temporary files, and caches. Derived files should be regenerable and should not be backed up or versioned redundantly unless the user marks them as important.
- **Shared asset library:** reuse compatible textures, materials, meshes, rigs, animation clips, audio, and code components across projects through references or content-addressed objects rather than copies. Check licenses, provenance, compatibility, and quality before reuse.
- **Incremental history:** store version deltas or deduplicated objects where safe, rather than duplicating entire projects for every snapshot. Rollback must remain reliable and must never overwrite unrelated user changes.
- **Cache and temporary-file budgets:** provide per-project and global cache limits, age/size policies, and safe automatic cleanup. The manager may automatically remove only confirmed regenerable cache/temp data; it must never silently delete source assets, approved deliverables, user imports, or the only copy of a version.
- **Archiving:** offer transparent project archiving and restoration using broadly supported compression, with integrity checks and clear visibility into archive contents. Keep active projects immediately usable and avoid mandatory proprietary formats.
- **Storage dashboard and forecasts:** report total CIE footprint, per-project and per-asset usage, duplicate space recoverable, cache/temp usage, dependency/model footprint, archive sizes, and estimated growth. Warn before configurable storage thresholds are exceeded. Show estimates as estimates, not measured facts.
- **User controls and safeguards:** allow storage limits, retention policies, compression/quality preferences, exclusions, archive locations, and cleanup approval settings. Default to safe automation; ask before irreversible actions and provide a recoverable trash/quarantine period where practical.
- **Storage efficiency evidence:** benchmark representative 2D, 3D, animation, audio, and code projects. Record raw versus stored size, compression ratio, deduplication savings, load/save time, quality deltas, and restore integrity. Never claim a space saving that has not been measured.

### 3.3 Project snapshots and rollback

- Capture snapshots before major or risky operations.
- Track changed files and associated generated assets.
- Compare snapshots and show concise diffs.
- Roll back failed or rejected operations without overwriting unrelated user changes.
- Preserve provenance linking each change to its task, tool, model, and approval.
- Integrate rollback with the audit trail and persistent storage.

### 3.4 Unified testing and verification runner

- Detect project type, available tools, and test commands.
- Select relevant checks by changed files and dependency graph; distinguish targeted checks from full-suite checks.
- Support Python tests/lint/types/security, JavaScript test runners, game project structural checks, 2D asset validation, 3D mesh profiles, animation metrics, and audio checks.
- Run checks with bounded resources, collect logs and artifacts, diagnose failures, propose or apply safe corrections, and repeat within a strict repair budget.
- Report exact commands, environment, test scope, passed/failed/skipped/blocked states, and evidence locations.
- A compile pass or targeted test pass must never be presented as full validation. Online installation checks must use a clean environment and record the resolved dependency closure.

### 3.5 Environment and dependency doctor

- Inspect Python/runtime versions, OS/architecture, installed dependencies, conflicts, disk space, permissions, configuration, and optional external applications.
- Detect missing or incompatible requirements before a workflow starts.
- Recommend the smallest safe fix and explain whether it requires network access or user approval.
- Operate locally after installation; explain when setup or an update needs network access, and never silently install or upgrade packages.

### 3.6 Project documentation and knowledge index

- Maintain architecture maps, module relationships, dependency records, build/test instructions, decisions, known issues, API references, and asset inventories.
- Update incrementally after verified changes.
- Preserve source provenance and mark inferred information as inference.
- Retrieve compact relevant slices instead of injecting whole documents into model context.

### 3.7 Verified template and component library

- Maintain reusable, tested building blocks for game systems, UI, inventory, character controllers, saves, dialogue, procedural generation, common coding patterns, and 2D/3D asset structures.
- Store compatibility metadata, tests, dependencies, licenses, examples, and quality status.
- Adapt verified components to a project rather than copying blindly.
- Do not promote unverified drafts into the reusable library.

## 4. Adaptive AI model and reasoning-effort router

This is a core planned capability, subject to the hard control boundary below.

### 4.1 Purpose

Automatically select the best actually accessible AI model and, independently, the appropriate reasoning-effort setting for each task or subtask. Optimize the quality/token/latency/cost tradeoff rather than always using the strongest or most expensive option.

### 4.2 Routing signals

Evaluate, when known:
- Task type and difficulty: code generation, debugging, architecture, research, reasoning, summarization, asset planning, structured extraction, or deterministic processing.
- Expected quality and reliability for that task, using benchmark and observed outcome data.
- Input/output token use and context-window requirements.
- Actual monetary cost, if any; paid options remain opt-in and are never required.
- Latency, throughput, rate limits, availability, and failure history.
- Supported tools, structured output, vision, code execution, and other required capabilities.
- Reasoning-effort controls and their measured quality/latency/token impact.
- Privacy policy, data locality, provider retention settings, and user restrictions.
- Model/provider compatibility and current capability metadata.

### 4.3 Routing behavior

- Classify the task and estimate required capabilities before choosing a route.
- Use deterministic CIE code without an AI call when that is more reliable and sufficient.
- Decompose complex tasks and route individual subtasks independently where practical.
- Begin with the least expensive/lowest-latency route likely to meet the quality target.
- Escalate model or reasoning effort when confidence is low, a validator fails, or the task exceeds the selected model's capability.
- Stop escalation when quality criteria pass, the user budget is reached, or no better permitted route exists.
- Cache suitable results and reuse verified work to avoid repeat model calls.
- Learn from benchmark results and accepted/rejected outcomes without treating noisy single outcomes as ground truth.

### 4.4 User controls

Provide explicit modes such as balanced, highest quality, lowest token use, fastest, privacy/local-only, and custom budget. Users can pin a model, provider, or effort level; exclude providers; set token/cost/time ceilings; and require confirmation before external calls. Defaults must honor the free-core constraint, user privacy settings, and explicit provider permissions; offline/local-only remains an optional user mode.

### 4.5 Critical limitation and truthful behavior

An MCP server cannot silently change the model selected inside the Claude application or control Claude's internal reasoning-effort setting unless the host explicitly exposes such controls. CIE may route its own calls among models/providers that the user has explicitly configured and authorized, including optional local models. For Claude itself, CIE can prepare compact context, select its own tools/workflows, and recommend a different host setting, but must not claim it changed Claude's model or effort. External paid APIs are optional integrations only and are never needed for the free core.

### 4.6 Placement

- **Phase 1:** capability registry, provider/model descriptors, routing policy schema, user budgets/preferences, deterministic no-AI route, and safe routing interface.
- **Phase 2:** task classifier, subtask decomposition, measured model/effort selection, escalation, caching, benchmark feedback, and performance learning.
- **Later phases:** expand task-specific benchmark coverage and integrations without weakening user controls or the free/offline baseline.

## 5. Creative-production capabilities

All of the following must have functional Tier-1 implementations owned by CIE. Optional libraries and external apps may improve workflows but cannot be prerequisites.

### 5.1 2D art and animation

- Procedural sprite and texture generation, editing, compositing, resizing, masking, palette conversion, and export.
- CIE-owned OKLCH palette lock, sprite composition, frame timing, sheet assembly, deterministic seeds, silhouette checks, and eight-direction sprite generation via orthographic projection/rasterization.
- Layered/editable workflows, tile/autotile validation, atlas layout checks, engine import profiles, transparent-edge checks, and animation consistency.
- SpriteKiln is optional only for its specific YAML format after wheel inspection and cross-platform tests; never a Tier-1 dependency.

### 5.2 3D modeling and assets

- CIE-owned procedural geometry, mesh construction, material/texture workflows, UV helpers, rigging heuristics, and glTF export/validation.
- Mesh repair staged through trimesh, manifold3d, Open3D, and CIE-owned non-convex hole filling.
- Mesh profiles: `closed_solid`, `character`, `terrain`, `foliage`, `open_surface`, and `multi_part`, each with explicit applicable/skipped checks, diagnostics, repair suggestions, and post-repair compliance.
- PyMeshFix is removed from Tier 1. PyMeshLab (GPL-3) is never Tier 1; optional Tier 3 only if independently installed and explicitly invoked.

### 5.3 Animation

- CIE-owned clip data model, keyframe authoring, forward kinematics, inverse-kinematics helpers, blending, retargeting, loop sealing, and glTF animation QA.
- Units: linear velocity mm/s, acceleration mm/s², jerk mm/s³; angular speed degrees/s or explicitly documented radians/s.
- Declared FPS is authoritative; default 30 FPS only when absent and report that assumption.
- Contact defaults: height epsilon 2 mm, speed epsilon 10 mm/s, 3 consecutive frames; project overrides allowed.
- Walk peak foot slip <50 mm/s and total <10 mm/contact; run <120 mm/s and total <25 mm/contact; jerk robust z-score 6; loop seam <1.5× median per-frame pose delta.
- Root motion is world-space; coordinate conversion is Y-up right-handed; handle ground plane and scale-aware thresholds.
- Golden animation tasks require human or optional Tier-2 VLM review of a contact sheet/GIF.

### 5.4 Audio

- CIE-owned procedural sound-effect presets, synthesis building blocks, audio analysis, loudness/peak checks, loop checks, manifests, and adaptive state-machine support.
- MIDI/stem workflows only with explicit soundfont and asset-license checks.
- Audio generation remains deterministic and local by default.

### 5.5 Game engines and project integration

- Godot full project writer and structural validation.
- Unity partial project writer and validation.
- Unreal project skeleton and validation.
- Engine-aware project detection, scene/resource references, missing-asset detection, project graph, compatibility checks, test/build orchestration, and artifact capture.
- External engines are optional execution targets, not prerequisites for CIE's core authoring and validation.

## 6. Coding and software-engineering capabilities

- Tree-sitter parsing and project indexing.
- FTS5 full-text retrieval, symbol graph, reciprocal-rank fusion, dependency-aware context selection, and source-aware citations.
- Language-server/compiler diagnostics when available; do not rely only on regex heuristics.
- Self-verification loops with bounded retries and clear stop conditions.
- Reproducible bug reports: environment, minimal reproduction, failing test, proposed patch, regression test, and rollback snapshot.
- Dependency-aware test selection while always identifying whether the full suite ran.
- Measure pass@1/pass@k on held-out tasks, regression rate, accepted patches, time-to-fix, and tokens per accepted fix.

## 7. Token and performance efficiency

- Server-side context/token budgets; use actual tokenizer when available and clearly label estimates otherwise.
- Retrieve only relevant source, project memory, assets, and dependency context.
- Deduplicate excerpts and avoid repeating already-known content.
- Cache deterministic results with invalidation on changed files, dependencies, tool versions, and configuration.
- Track cold/warm latency, p50/p95, CPU/RAM, output size, cache hit rate, stale-result rate, and tokens per successful task.
- Prefer deterministic algorithms and local computation over AI calls where they meet the task's quality bar.

## 8. Memory and provenance

- Separate user-confirmed facts, project facts, observed tool output, hypotheses, and stale/contradicted facts.
- Record provenance, timestamps, confidence, project scope, and source paths.
- Detect contradictions and support expiry/review policies.
- Keep memory distinct from artifact bytes; link memory to versioned artifacts and evidence.
- Retrieval must respect project boundaries and permissions.

## 9. Frozen v2.3.2 dependency and security policy

- Tier-1 dependencies require exact pins, provenance, transitive dependency review, wheel availability, hashes, and license/security approval.
- Allowlist: MIT, MIT-CMU, BSD-2-Clause, BSD-3-Clause, Apache-2.0, ISC, PSF-2.0; LGPL-2.1-only is conditional and requires explicit review. GPL/AGPL are prohibited in Tier 1.
- Use `pip-audit`, `pip-licenses`, and `pipdeptree`; retain evidence and review native bundled components, including `soundfile`/`libsndfile` LGPL obligations.
- Maintain `deps/registry.yaml`; monthly security review and quarterly minor-version review; no automatic major upgrades.
- Optional wheelhouses may cache the CIE wheel and pinned direct/transitive dependencies with SHA-256 manifests, but are not required for Phase 0 or normal installation.
- Standalone bundles must embed the runtime and start without system Python.
- Build-system dependencies are separately pinned and provisioned; source builds must not silently fetch PEP 517 requirements.

## 10. Frozen phase sequence and acceptance gates

### Phase 0 — bug fixes, online packaging, CI, dependency registry

- Fix `vault.py` truncation, missing modules, duplicate Blender call, gitleaks temporary-file leak, and job-future leak.
- Add/verify `pyproject.toml`, CI, `deps/registry.yaml`, dependency audit/license gates, and normal online installation.
- Build native standalone bundles on hosted Windows, Ubuntu, and macOS runners; embed Python and verify startup without a system Python installation.
- Run direct/transitive vulnerability and license audits in a clean environment; review conditional/native license obligations.
- Run tests and CLI smoke checks across supported Python versions and operating systems.
- Optional wheelhouses and disconnected VM tests are deferred unless the owner later requires disconnected installation.
- **Current status: NOT COMPLETE.** Source tests pass, but dependency/license audits, native standalone builds, and actual remote CI evidence remain open.

### Phase 1 — foundations

- Pydantic schemas, async executor, server-side token governor, anchored audit, memory tiers, permission engine.
- Persistent artifact store, SQLite metadata, content addressing, versioning, snapshots/rollback, project boundaries.
- Storage manager foundations: usage accounting, duplicate detection, safe cache/temp classification, configurable budgets, and non-destructive cleanup policies.
- Environment doctor, unified verification-runner foundations, compact MCP workflow surface.
- Model/provider capability registry, user routing preferences/budgets, and routing-policy interface; no unsupported host-model control claims.

### Phase 2 — coding intelligence, retrieval, orchestration, adaptive routing

- Tree-sitter index, FTS5 + symbol graph retrieval + reciprocal-rank fusion.
- Self-verification loop, task decomposition, dependency-aware context and tests.
- Adaptive model/reasoning-effort router, measured selection, escalation, caching, performance learning, and opt-in provider/local-model adapters.
- Project knowledge index, verified reusable component library, documentation maintenance.

### Phase 3a — 2D

- CIE-owned sprite composition, palette lock, sheet assembly, edit workflows, structural validators, tile/atlas/texture validation.
- SpriteKiln optional only.
- Texture and sprite storage profiles: preserve editable/lossless masters, deduplicate repeated atlas content where safe, and create optimized runtime exports with explicit quality settings.

### Phase 3b — 3D

- CIE-owned procedural generators, staged repair pipeline (trimesh + manifold3d + Open3D + CIE non-convex hole filling), rigging heuristics, glTF export/validation.
- Explicit mesh profiles and quality evidence.
- 3D asset storage profiles: preserve editable source meshes and high-quality masters; generate appropriately compressed delivery meshes and textures; support optional mesh/texture compression only when target compatibility and visual quality are validated.

### Phase 3c — animation

- CIE-owned clip model, FK/IK, keyframe authoring, blending, retargeting, loop sealing, and the frozen glTF QA metrics.

### Phase 3d — audio

- CIE-owned SFX presets, music/audio policy, adaptive state machine, analysis, and manifests.
- Preserve original/master audio and generate compressed delivery copies only within declared quality targets; deduplicate shared source audio and avoid retaining redundant intermediate renders.

### Phase 4 — engine integrations

- Godot full project writer, Unity partial project writer, Unreal skeleton, structural validators, round-trip tests, and engine-specific project intelligence.

## 11. Definition of Done

A capability is not complete until all applicable requirements pass:
- Unit and integration tests.
- Offline integration where the capability is claimed to work offline.
- Structural benchmarks and held-out reference benchmarks where available.
- Golden visual/audio review for 2D, 3D, animation, and audio outputs.
- Determinism and repeatability checks.
- Security, permission, resource, and audit-chain tests.
- Dependency/license gate with no prohibited Tier-1 packages.
- Latency, memory, output-size, disk-footprint, compression-quality, deduplication, and token-budget evidence.
- Storage tests prove source assets are never removed by automatic cache cleanup, archives restore with integrity, snapshots remain recoverable, and compression stays within declared quality thresholds.
- Cross-platform packaging and installation evidence for supported targets.
- Clear reporting of passed, failed, skipped, blocked, and untested cases.

A test skipped because a tool or dependency is missing is not a pass. A local run is not proof of Windows/macOS behavior. A project-only wheel install is not proof of a complete offline runtime. A model-router decision is not successful unless the selected route was actually available, authorized, and within budget.

## 12. Current execution rule

Continue finishing Phase 0 only. Do not start Phase 1 implementation or mark Phase 0 complete until every mandatory Phase 0 gate has actual evidence. The adaptive router, persistent artifact storage, and storage-efficiency manager are part of the master product plan, but their implementation remains sequenced behind the frozen Phase 0 acceptance requirements. Storage optimization must not be used to bypass source preservation, rollback, licensing, or quality gates.


## Owner-approved installation policy update (2026-11-26)

The owner explicitly relaxed the requirement for normal offline installation. CIE is now online-first for installation and updates, while remaining local-first at runtime and free of mandatory paid APIs or hosted CIE services. This supersedes the earlier offline-first product constraint. Wheelhouse creation, full disconnected VM testing, and offline source-build provisioning are optional follow-on work, not Phase 0 blockers. CI should instead use hosted Windows, Ubuntu, and macOS runners for native tests and standalone builds.


## Owner-approved storage-efficiency addition (2026-11-26)

The owner approved making storage minimization a first-class CIE requirement. The plan now includes format-aware compression, content/chunk deduplication, shared asset references, incremental history, safe cache cleanup, project archiving, storage forecasts and limits, and measurable quality/integrity acceptance tests. Automatic cleanup is restricted to confirmed regenerable data; original assets and user deliverables are protected.
