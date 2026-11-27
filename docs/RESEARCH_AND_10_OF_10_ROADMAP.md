# CIE quality roadmap: research, measurable targets, and game-development specialization

**Release reviewed:** 1.2.0  
**Hardening release:** 1.2.1  
**Design constraints:** local-first, no paid AI API, no automatic model downloads, optional local models, low setup burden.

**v1.2.1 validation in this environment:** 46 pytest tests passed and 6 skipped because optional external tools were not installed; `unittest` also passed all 50 collected tests with the same 6 skips. See `CHANGELOG.md`.

## What “10/10” should mean

A 10/10 claim is not supportable from a code review alone. CIE should earn a score through repeatable benchmarks, security tests, cross-platform packaging, and real game-project evaluations. The targets below are release gates, not claims that v1.2.1 has already achieved them.

## Confirmed fixes shipped in 1.2.1

- Changed fresh-install autonomy from `autonomous` to `assist`.
- Migrated pre-1.2.1 configuration files to `assist`, correcting the old autonomous default even when the user had not explicitly opted in.
- Routed memory operations through the capability registry. Read operations are read-only; writes require owner approval in assist mode; restore/import/export require explicit owner approval in every autonomy mode. Observe mode remains strictly read-only even if a tool has a saved approval.
- Made the owner dashboard able to approve registered destructive capabilities deliberately, with a warning that the approval is persistent.
- Added recursive secret redaction to structured tool outputs, including sensitive dictionary fields.
- Added job timeout state and cooperative cancellation. The API now reports the important limitation: Python cannot safely terminate arbitrary running threads, so workers must cooperate.
- Added generic capability argument validation for required fields, primitive/list/object types, enum choices, numeric ranges, unknown fields, and a 2 MB serialized-input ceiling.
- Replaced raw byte-only smart-context budgeting with token estimates, with the estimator reported in every result and legacy byte-budget compatibility retained.
- Hardened memory version restore, import, and export against path traversal, symlinks, and overlapping source/destination paths.
- Added regression tests for these behaviors.

## Research-backed design rules

### 1. MCP tools must be least-privilege and permission-aware

Treat every tool call as a security boundary. Validate schemas and bounds, keep sensitive actions behind explicit approval, sanitize tool output before returning it to the model, and constrain execution. The OWASP MCP Security Cheat Sheet specifically recommends least privilege, parameter validation, approval for sensitive actions, output validation, and resource controls. The NSA/CISA MCP security guidance similarly emphasizes validating inputs and constraining tool execution. Sources:

- [OWASP MCP Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/MCP_Security_Cheat_Sheet.html)
- [NSA: MCP Security Design Considerations (2026)](https://media.defense.gov/2026/Jun/02/2003943289/-1/-1/0/CSI_MCP_SECURITY.PDF)
- [MCP protocol specification](https://github.com/modelcontextprotocol/modelcontextprotocol/tree/main/docs/specification)

**10/10 gate:** every capability has a declared risk class, validated input schema, output-size limit, timeout/resource policy, and tests proving that unauthorized operations fail closed. No tool can self-approve through model-supplied arguments.

### 2. Timeouts must reflect the execution model

Python's subprocess APIs support explicit timeouts, but thread-based work cannot be safely force-killed. A timeout flag is not equivalent to terminating the work. CIE should use subprocess isolation for untrusted or non-cooperative long-running jobs and cooperative cancellation for trusted Python functions. Sources:

- [Python subprocess documentation](https://docs.python.org/3/library/subprocess.html)
- [Python asyncio subprocess documentation](https://docs.python.org/3/library/asyncio-subprocess.html)

**10/10 gate:** all external processes have bounded execution time, bounded stdout/stderr, and cleanup behavior; jobs distinguish `cancelled`, `cancelling`, `timed_out`, `failed`, and `completed`; no late worker result can overwrite a terminal state.

### 3. MCP tools should be discoverable, bounded, and easy to compose

The MCP documentation describes the tool-discovery and tool-execution loop. Compact tool surfaces are useful, but tool descriptions and schemas need to be specific enough for the model to choose the right capability and pass correct arguments. Sources:

- [MCP build-client guide](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/docs/2026-07-28/develop/build-client.mdx)
- [MCP protocol overview](https://github.com/modelcontextprotocol/modelcontextprotocol/tree/main/docs/specification/2026-07-28/basic)

**10/10 gate:** descriptions include purpose, constraints, examples, side effects, expected output, and failure modes; discovery supports multi-term relevance ranking; batch calls remain read-only unless a separately approved workflow is used.

## Roadmap to a genuinely elite game-development assistant

The current project has useful building blocks for code analysis, Godot linting, image inspection, sprite atlases, Blender, mesh validation/repair, audio analysis, procedural sound, and external creative tools. That is not yet equivalent to a full game-production specialist. The next work should be built as validated pipelines, not as a pile of loosely related tools.

### A. Game-project intelligence

1. Add engine-aware project detection for Godot, Unity, Unreal, and common custom engines.
2. Build a project graph linking scenes, scripts, assets, shaders, audio events, and dependencies.
3. Add engine-specific diagnostics and version compatibility checks.
4. Add scene/resource reference validation and missing-asset detection.
5. Add build/test orchestration with full-suite release checks and artifact capture.

**Benchmarks:** exact engine detection on a sealed project set; missing-reference precision/recall; build success rate; false-positive rate; time to identify the root cause of seeded project failures.

### B. Coding and debugging intelligence

1. Use language-server/compiler diagnostics where available instead of relying only on regex/static heuristics.
2. Add dependency-aware test selection, but always distinguish selected tests from full-suite validation.
3. Add reproducible bug reports: minimal reproduction, environment capture, failing test, proposed patch, regression test, and rollback snapshot.
4. Evaluate patch quality on hidden tasks; do not score a patch as successful merely because it compiles.
5. Add repair-loop limits and stop conditions to prevent endless autonomous edits.

**Benchmarks:** pass@1 and pass@k on held-out coding tasks; regression rate; tests passed; patch acceptance; time-to-fix; tokens per accepted fix.

### C. 2D art, textures, and animation

1. Add layered sprite workflows with palette control, pixel-grid validation, animation timing, and export presets.
2. Add texture-map validation for dimensions, color space, channel packing, mipmaps, and compression targets.
3. Add tileset/autotile validation and atlas layout checks against engine constraints.
4. Add batch import/export profiles for the target game engine.
5. Keep generated art optional and local; do not make paid APIs or automatic model downloads mandatory.

**Benchmarks:** pixel alignment, transparent-edge defects, atlas packing efficiency, import success, animation frame consistency, and human-rated asset usefulness.

### D. 3D modeling, animation, and rendering

1. Extend mesh QA to topology, normals, UV coverage, material slots, scale, transforms, rig weights, and LOD consistency.
2. Add Blender scene validation, render presets, asset naming rules, and batch rendering.
3. Add glTF/FBX export validation and engine round-trip tests.
4. Add shader compilation and render-diff tests for supported engines.
5. Use isolated subprocesses/containers for complex external rendering jobs; never treat a timeout flag as process termination.

**Benchmarks:** import/export round-trip integrity, render reproducibility, frame time, draw calls, polygon/texture budgets, and visual regression thresholds.

### E. Sound design and music

1. Add audio event-bank validation, loudness targets, clipping detection, loop-point checks, and sample-rate/bit-depth presets.
2. Add procedural SFX building blocks with parameterized envelopes, modulation, filters, and deterministic seeds.
3. Add MIDI and stem workflows with explicit soundfont/licensing checks.
4. Add engine audio-event manifest validation and missing-reference detection.
5. Keep audio generation deterministic and local by default.

**Benchmarks:** loudness/peak compliance, loop seam detection, event coverage, export integrity, and blind human ratings for usefulness and fit.

### F. Speed and token efficiency

1. Benchmark every capability with cold/warm latency, CPU/RAM, output size, and cache hit rate.
2. Track tokens per task using the target model's actual tokenizer where available; label fallback estimates clearly.
3. Rank context by task relevance, recency, confidence, dependency proximity, and source authority.
4. Deduplicate overlapping file excerpts and memory notes before returning context.
5. Add cache invalidation tests for changed files, changed dependencies, changed tool versions, and changed configuration.

**Benchmarks:** tokens per successful task, context precision, retrieval recall, p50/p95 latency, peak memory, cache hit rate, and stale-result rate.

### G. Memory quality

1. Separate user-confirmed facts, project facts, observed tool output, hypotheses, and stale/contradicted facts.
2. Store provenance, timestamps, confidence, project scope, and source paths for every memory item.
3. Add contradiction detection and expiry/review policies.
4. Require explicit user confirmation before storing personal preferences or sensitive facts.
5. Provide export, backup, restore, and deletion verification with audit logs.

**Benchmarks:** retrieval precision/recall, contradiction rate, stale-memory rate, provenance coverage, and successful correction/deletion tests.

### H. Installation and platform support

1. Ship a tested one-click installer for Windows and equivalent documented installers for macOS/Linux.
2. Verify clean install, upgrade, rollback, uninstall, and data-preservation behavior on clean virtual machines.
3. Pin dependencies and produce an SBOM plus reproducible build metadata.
4. Test supported Python versions and OSes in CI.
5. Keep optional heavyweight tools optional; never silently download models or external binaries.

**Benchmarks:** clean-install success rate, setup time, upgrade success, uninstall cleanliness, reproducible build comparison, and vulnerability scan results.

## Release gates before calling any area “10/10”

- 100% of registered capabilities have input-validation, permission, failure-mode, and output-bound tests.
- No known critical/high security findings remain open.
- 100% of external processes have bounded time and output, and are terminated/cleaned up on timeout.
- At least 95% clean-install success across supported OS/Python combinations in CI.
- Every supported game engine has a representative fixture project and an automated end-to-end audit/build test.
- Every major creative workflow has a deterministic fixture and an objective quality gate.
- Performance and token metrics are published against a baseline; no “faster” or “smarter” claim without measured improvement.
- All model-dependent capabilities remain optional, local, license-checked, and off by default unless the user opts in.

## Important limitation

CIE can improve Claude's workflow, access to project information, memory, tool execution, and creative pipelines. It cannot directly increase Claude's underlying model weights or guarantee that Claude will reason better on every task. Genuine intelligence gains must be demonstrated as higher success rates on held-out tasks, not inferred from the number of tools installed.

## Clarification: workflow approvals

The workflow runner intentionally re-checks permission for every nested capability instead of inheriting one outer workflow approval. This is a least-privilege safeguard: approving a workflow container must not silently approve every write, execution, or destructive operation inside it. Each non-read step must have its own owner-side approval (and destructive steps always need explicit approval). This behavior is retained rather than “fixed” by broad approval propagation.