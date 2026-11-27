# Security policy

## Supported development line

CIE v2.3.2 development builds are not production releases. Report suspected vulnerabilities privately to the project maintainer rather than publishing exploit details before a fix is available.

## Security invariants

- Keep autonomy at `assist` by default; `observe` must remain read-only.
- Never bypass capability permission checks from MCP-facing handlers.
- Resolve filesystem paths against approved project roots and reject traversal and symlink escapes.
- Use subprocess argument arrays with `shell=False`; never interpolate untrusted input into shell commands.
- Redact secrets before returning tool output to an LLM client.
- Treat temporary security-scanner reports as sensitive and delete them in `finally` blocks.
- Do not include GPL/AGPL dependencies in the Tier-1 install.
- Audit direct and transitive dependencies, wheel contents, hashes, provenance, and known vulnerabilities before release.

## Dependency updates

Use exact pins for Tier 1. Review security advisories monthly and minor dependency updates quarterly. Do not automatically accept major-version upgrades. A dependency with unknown provenance, unresolved licensing, or missing supported wheels is blocked from Tier 1.
