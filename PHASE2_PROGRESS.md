# CIE Phase 2: Coding and Development Intelligence

**Status: built and locally verified (2026-11-27).** Focus languages (owner decision): Luau, JavaScript/HTML, and Java/Kotlin, with Python still supported. No new dependencies: scanning uses CIE's own comment- and string-aware scanner (`cie/codescan.py`), not third-party parsers.

## What was built

| Part | Module | Capabilities |
|---|---|---|
| 2.1 Code intelligence | `cie/codescan.py`, `cie/codeintel.py` | `code.overview`, `code.callers`, `code.graph`, `code.unused`, `code.duplicates`, `code.complexity`, `code.api` |
| 2.2 Review and fixes | `cie/review.py` | `code.review`, `code.rules`, `code.fix_preview`, `code.fix_apply` |
| 2.3 Testing | `cie/devtools.py` | `test.detect`, `test.run` |
| 2.4 Safe execution | `cie/devtools.py` | `workspace.create`, `workspace.diff`, `workspace.promote`, `workspace.discard`, `workspace.list` |
| 2.5 Workflow | `cie/devtools.py` | `dev.workflow` |

There are now 145 capabilities (126 after Phase 1).

### Code intelligence
- The unified index (now version 4, so each project re-indexes once automatically) stores, per definition: its line span, branch complexity, parameter count, and whether it is exported. It also stores every call site.
- Comments and strings are blanked before matching, so `"function x()"` inside a string or a comment is never mistaken for code.
- `code.callers` names the function each call sits in, for example "`damage` calls `Util.clamp`".
- `code.graph` resolves Luau `require(...)` chains, including `game:GetService(...):WaitForChild("X")`, and also JS relative imports, Java/Kotlin package imports, and Python modules. It reports two-way dependencies and outputs Mermaid text.
- `code.unused` skips engine callbacks (Roblox `on*` handlers, `onInitialize`, `_ready`, and others), functions passed as values, and exports. It labels results as inferred.

### Review rules (22)

| Language | Rules |
|---|---|
| Luau | `--!strict`, deprecated `wait`/`spawn`/`delay`, `game.Service` → `GetService`, `while true` loops that never yield, unvalidated `OnServerEvent` values (exploit risk) |
| JavaScript | loose `==` (except `== null`), `var`, `innerHTML` with built strings, `eval`, unprotected `JSON.parse(localStorage...)`, `setInterval` game loops |
| Hearth | unguarded `Hearth.unlock`, IndexedDB or cookies in Hearth games |
| Java/Kotlin | string `==`, empty `catch`, `System.out`/`println` instead of a logger, `!!`, **client imports in common code (crashes dedicated Minecraft servers)** |

Mechanical fixes show as a unified diff, and nothing is applied until the owner approves (`code.fix_apply`). A fix is skipped if its line changed after the preview. Windows line endings are kept, and every apply snapshots the files first, so it can be undone.

### Testing and workspaces
- `test.detect` finds pytest, npm/node tests, Gradle (`gradlew test`), TestEZ specs, and CIE's static checks. TestEZ is reported as needing Roblox Studio; that's Phase 4.
- `test.run` runs from a fixed command table, never from caller input, with no shell and a timeout. It returns pass and fail counts and the key error lines. It's an exec capability, so it asks for approval and snapshots the project first.
- Workspaces copy a project to `CIE_HOME/workspaces`, let changes be tested there, show the diff, and copy chosen files back with a snapshot. Discarding deletes only the copy.
- `dev.workflow` gives the 10-step plan from the master plan, with a model and effort pick, and routes hard tasks to a workspace.

## Verification (Linux sandbox, Python 3.13)
- `python -m pytest -q`: 161 passed, 6 skipped. 2 failed, both `trimesh` tests, which can't run in the sandbox.
- Phase 1 and Phase 2 tests together: 0 failures in 8 repeated runs.
- `test.run` was exercised against a real pytest suite.

## Known limits
- Scanning is pattern-based, not a full parser. Unusual formatting, such as a class and its methods on one line, can hide a definition.
- Callers, unused code, and graph edges are matched by name, so two functions sharing a name in different modules are treated as the same function.
- TestEZ tests need Roblox Studio (Phase 4). Gradle runs need a JDK on the PC.
