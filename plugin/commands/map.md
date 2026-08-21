---
description: Map the codebase — imports, cycles, dead code, hotspots
argument-hint: [module]
---
Use the `codebase_map` tool from the **github-autopilot** MCP server.

If `$1` is given, treat it as a dotted module path (e.g. `app.core.config`) and
pass it as `module` to get that module's dependants — who breaks if it changes.
Otherwise ask for the whole-codebase summary.

Report, in this order, and lead with whichever is non-empty:

1. **Import cycles.** These are design defects, not style: a cycle means
   neither module can be understood or tested alone. Name the full ring.
2. **Modules nothing imports.** Each is a decision the maintainer owes: delete
   it, or declare it an entrypoint. Do not describe an unreachable module as
   "unused but probably fine" — a module with passing tests and no importer
   proves nothing about the product.
3. **Hotspots** — the most depended-on modules, with their line counts. These
   are where a change is most expensive, so they are where tests pay best.
4. Totals: modules, imports, lines.

The map is derived from the AST and never imports the code it reads, so it is
safe to point at a repository you do not trust.
