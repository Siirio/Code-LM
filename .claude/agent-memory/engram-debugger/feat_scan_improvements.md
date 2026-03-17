---
name: Scan hardening — multi-pom, single-file, JS/TS layer detection, auto-rule fallback
description: Six scan-pipeline hardening changes in project_scanner.py covering all project types
type: project
---

## Original fixes (sessions 1-3)
See git history for Qdrant dim guard, import-based stack detection, and initial auto arch rules.

## Session 4 hardening — all six requirements applied to project_scanner.py

### 1. Multi-pom.xml / multi-build.gradle (monorepos, corrupted files)

New helper `_find_build_files(root_path, filename, max_depth=2)`:
- Uses `os.walk` bounded to `max_depth` levels deep.
- Returns paths sorted shallowest-first so root-level file always wins.
- Wrapped in `try/except OSError`.

`_detect_stack()` now uses `_find_build_files` for pom.xml, build.gradle(.kts), package.json, and go.mod. Each file read is individually wrapped in `try/except (OSError, json.JSONDecodeError, Exception)` so malformed XML/JSON can never crash detection. The function always returns a valid `stack` dict.

### 2. Single-file projects

`scan_project()` (now `_scan_project_impl`) has no division-by-zero risk — all aggregations are len() calls or set comprehensions that work on empty or 1-element lists. The outer `scan_project()` wraps the impl in `try/except` and re-raises, giving a clean log entry for any unexpected failure.

### 3. Auto-rule always saves at least one rule

`_try_add_rule()` inner async helper tracks `rules_saved` via nonlocal. Rule selection cascade:
1. Controller+Service+Repository → two layered-arch rules (unchanged).
2. Controller+Service only → business-logic rule (unchanged).
3. View-only (no controller/service) → "Components should not contain business logic" (warning).
4. Controller-only → "Keep functions under 50 lines" (warning).
5. Fallback (rules_saved == 0): if framework is React/Vue/Angular/Svelte/Remix/Next.js → component rule; else → "Follow single-responsibility principle".

### 4. JS/TS: React, Express, hook detection

`_parse_ts_js_file()` now returns an extra `layer_hints: list[str]` key:
- `has_jsx` (any `<Component` or `</tag>` pattern) or `has_react_class` (extends React.Component) or `has_default_func_component` → hint "View".
- `has_express_routes` (`app.get/post/...` or `router.get/...`) → hint "Controller".
- `has_hooks` (`useState`, `useEffect`, `useCallback`, `useMemo`, `useRef`) → hint "View" (if not already set).

Layer resolution priority in `scan_project`: (1) path hint, (2) layer_hints[0], (3) class-name suffix, (4) "Util".

### 5. `_classify_layer_from_path()` expanded

Old: only `/components/` and `/pages/` → "Component".
New:
- `/components/`, `/pages/`, `component` or `components` segment → "View"
- `/routes/`, `/route/`, `routes`/`route`/`controllers` segment → "Controller"
- `/store/`, `/redux/`, `/context/` → "Service"
- `/hooks/`, `/hook/` → "Util"

Return type remains `str | None` — None when no pattern matched.

### 6. `_detect_stack()` — JS framework additions

package.json now detects: Vue.js (`vue`), Angular (`@angular/core`), Svelte (`svelte`/`@sveltejs/kit`), Remix (`@remix-run/node` or `@remix-run/react`). Build-tool refinement: `vite` in deps → `"Vite"`, `webpack`/`webpack-cli` → `"webpack"`. Import-based fallback extended with `react` and `vue` prefixes.

`arch_pattern` in summary_text now has a "Frontend (framework)" branch for view-only projects.

**Why:** Projects without all three backend layers (Controller+Service+Repository) previously got no auto-rules and the arch_pattern was "unknown", causing the LLM to misinterpret the project as unscanned. Frontend, script, and single-file projects are now first-class citizens of the scanner.

`add_rule()` signature: `(project_id, name, description, severity="error") -> dict`
