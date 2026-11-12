# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**Audubon IBA fork.** This is a National Audubon Society Important Bird Areas fork of the OpenContext MCP server framework (forked from the San Diego City GIS fork). It serves Audubon's public IBA feature service via the `audubon_iba` plugin (see `config.yaml`).

Unlike the sibling GIS forks, this one fronts a **single fixed ArcGIS FeatureServer with a known schema** — not a portal or services directory. There is nothing to crawl, so all the catalog-discovery machinery is gone: no `search_datasets`, no `get_aggregations`, no catalog manifest, no dataset-id resolution. Every tool addresses layer 0 (or one of its related tables) directly.

```
https://services1.arcgis.com/lDFzr3JyGEn5Eymu/arcgis/rest/services/iba_polygons_public/FeatureServer
  layer 0   iba_polygons_public  -- the main IBA polygon record
  tables    3 criteria_site   4 criteria_species   5 habitat    6 landuse
            7 observation     8 ownership          9 site_description
           10 species        11 threat
```

Key invariants of the `audubon_iba` plugin:

- **Six tools, no discovery** — `find_ibas_near_point`, `search_ibas`, `get_iba_details`, `get_iba_related_records`, `get_iba_boundary`, `list_distinct_values`, under the `audubon_iba__` prefix.
- **Geometry is opt-in** — every tool but `get_iba_boundary` sends `returnGeometry=false`, so the shared pager (`_query_layer_paged`) hardcodes it. `get_iba_boundary` has its own request path and simplifies server-side with `maxAllowableOffset` (in *degrees*, since `outSR=4326`).
- **WGS84 hard contract** — the polygons are authored in EPSG:3857 (Web Mercator); every query sets `inSR=4326`/`outSR=4326` and lets the service reproject. Without `inSR`, WGS84 coords are read as Web Mercator metres and silently match nothing. `find_ibas_near_point` adds a server-side buffer (`distance` + `units=esriSRUnit_Kilometer`).
- **Runtime schema introspection** — layer 0's fields, coded-value domains, record cap, and relationship ids are read from the service at runtime (`?f=json`) and cached per instance. Nothing is hardcoded beyond the fields the tools name. Related tables are introspected separately, so their dates/domains decode against *their own* schema.
- **relationshipId != relatedTableId** — `queryRelatedRecords` takes the relationship's `id` (0–8), not the related table's id (3–11). The plugin resolves the id from live metadata by relationship *name*; hardcoding the number would silently query the wrong table.
- **site_id is not objectid** — `queryRelatedRecords` keys on the origin layer's `objectid`, so `get_iba_related_records` resolves `site_id -> objectid` first (which doubles as the existence check).
- **Pagination** — `maxRecordCount` is 2000; both `/query` and `/queryRelatedRecords` page with `resultOffset`/`resultRecordCount`.
- **No free-form SQL surface** — callers pass values (`state`, `priority`, `flyway`, `name`), never a WHERE clause. Values are quote- and LIKE-wildcard-escaped into fixed predicates, and the composed clause still goes through `WhereValidator` (shared from `plugins/arcgis/where_validator.py`). Field names in `list_distinct_values` are validated against the live schema before interpolation.
- **Auth-free** — the public service is anonymous. The `token` config key exists only so the same plugin could front a secured mirror.

### The designation caveat (non-negotiable)

An IBA is a science-based conservation **priority** (BirdLife/Audubon criteria, ranked Global / Continental / State in `iba_priority`). It carries **no legal or regulatory force**: it does not protect land, restrict use, or trigger permitting, and its boundaries are advisory rather than authoritative property lines. This is stated in the server-level `instructions` (config.yaml), repeated in tool descriptions, and appended as a footer to list/detail output. Do not weaken it.

### Composing with the eBird MCP server (the silent-failure seam)

The two servers are meant to chain — find an IBA, then pull live sightings — but they share **no keys**, and the places where they look like they do are where wrong answers come out silently. Verified against Mission Bay (`site_id` 202) on 2026-07-13.

- **`species_code` is NOT eBird's `speciesCode`.** Audubon's species table is frozen at a pre-2014 taxonomy. Most of its codes happen to coincide with eBird's (`brant`, `marwre`, `savspa`), which is precisely what makes the rest dangerous. Site 202 is designated for "Clapper Rail"/`clarai` — but `clarai` is eBird's code for *Rallus crepitans*, an Atlantic/Gulf bird absent from California. The western population was split off as Ridgway's Rail (`ridrai1`) in 2014. Passing the code straight through queries a **different species** and returns a plausible "no records" for a rail that is present. Resolve by scientific name against eBird's live taxonomy; `_KNOWN_TAXONOMY_SPLITS` carries curated notes for the landmines, and is a hint, not an authority.
- **`sci_name` is only the epithet** (`longirostris`); the genus is in `iba_genus`. `_compose_binomial` rebuilds the whole name — which may itself be stale taxonomy, so it is a lookup key for eBird, not the answer.
- **`ebird_link` is not a join key.** It is a human-facing GuideMe URL, and the `US-CA_202` inside it is an Audubon IBA id that every eBird tool rejects. Do not sell it as an API identifier.
- **The centroid is the only bridge that works.** `get_iba_details` emits `lat`/`lng` plus a `suggested_radius_km` (equal-area circle from `acres`) because eBird's nearby default of 25 km is metro-scale — against Mission Bay's ~12 km² it over-scans ~160×. For a true in-boundary audit rather than a radius, use `get_iba_boundary` and filter hotspot coordinates against the rings.
- Prefer any of this over the `observation` table, whose records are historical.

## Server-level instructions

The MCP `initialize` response carries an `instructions` field, sourced from the top-level `instructions:` key in `config.yaml` and wired through `MCPServer(plugin_manager, config)`. It is where interpretive guardrails live, so they do not have to be repeated in every tool description. `serverInfo.name` comes from `server_name` in the same config (defaulting to `opencontext` when no config is passed).

## Build & Development Commands

```bash
# Install dependencies (uv preferred, pip fallback)
uv sync                              # or: pip install -r requirements.txt

# Run local MCP server (no Lambda needed)
python3 scripts/local_server.py      # Serves on http://localhost:8000/mcp
# On Windows, set PYTHONIOENCODING=utf-8 first (the script prints emoji).

# Validate config
python3 -c "from core.validators import load_and_validate_config; load_and_validate_config('config.yaml')"

# Smoke test a deployment (or a local server). Ends on the verification
# queries: a 40km buffer around Anchorage, then a species traversal on a
# site it returns.
python3 scripts/smoke_prod.py http://localhost:8000/mcp

# Tests
uv run pytest tests/ -n auto                                    # All tests, parallel
uv run pytest tests/test_audubon_iba_plugin.py -v               # This fork's plugin
uv run pytest tests/ --cov=core --cov=plugins --cov-report=term-missing  # 80% minimum

# Linting (ruff)
uv run ruff check core/ plugins/ server/ tests/ --fix
uv run ruff format core/ plugins/ server/ tests/

# Deploy to AWS
./scripts/deploy.sh --environment staging
```

## Architecture

**Core rule: One Fork = One MCP Server.** Each deployment runs exactly ONE plugin, enforced at config validation time (`core/validators.py`) and at runtime (`PluginManager.load_plugins()`). This fork runs `audubon_iba`. The other built-in plugins (`ckan`, `arcgis`, `socrata`, `anchorage_gis`) remain in `plugins/` as framework code but are disabled; `plugins/arcgis/catalog.json` is a leftover San Diego crawl artifact and is unused here.

**Request flow:**
```
Claude (stdio) → Go client (client/) or stdio_bridge.py → HTTP POST /mcp
  → Lambda (server/adapters/aws_lambda.py) or local_server.py
  → server/http_handler.py → core/mcp_server.py (JSON-RPC 2.0)
  → core/plugin_manager.py → Plugin → External API
```

**Key modules:**
- `core/interfaces.py` — Abstract bases: `MCPPlugin`, `DataPlugin`, plus `ToolDefinition`, `ToolResult`, `PluginType` enum. The IBA plugin implements `MCPPlugin` directly, not `DataPlugin` — it has no dataset/resource concept to satisfy.
- `core/plugin_manager.py` — Discovers plugins by scanning `plugins/` and `custom_plugins/` for `plugin.py` files. Registers tools with the `pluginname__toolname` prefix. Routes `tools/call` to the correct plugin.
- `core/mcp_server.py` — Handles MCP JSON-RPC methods: `initialize` (incl. `instructions`), `tools/list`, `tools/call`, `ping`
- `core/validators.py` — Loads config from `config.yaml` (local) or `OPENCONTEXT_CONFIG` env var (Lambda). Enforces the single-plugin rule.
- `server/adapters/aws_lambda.py` — AWS Lambda entry point (handler: `server.adapters.aws_lambda.lambda_handler`)
- `server/http_handler.py` — Cloud-agnostic HTTP handler shared by Lambda and local server
- `stdio_bridge.py` — Python stdio-to-HTTP bridge for connecting Claude Desktop/Code to the local server

## Plugin Development

New plugins must implement `MCPPlugin` (or `DataPlugin` for catalog-style data sources). Place in `custom_plugins/<name>/plugin.py`. The class must define `plugin_name`, `plugin_type`, `plugin_version` and implement `initialize()`, `shutdown()`, `get_tools()`, `execute_tool()`, `health_check()`. Tool names are auto-prefixed — return bare names from `get_tools()`.

## Configuration

Copy `config-example.yaml` to `config.yaml`. Enable exactly one plugin. Config does **NOT** support `${ENV_VAR}` substitution: `core/validators.py::load_and_validate_config` is a plain `yaml.safe_load`, so a `${VAR}` reaches the plugin as that literal string. Read secrets from `os.environ` in the plugin's `initialize()` instead, and never put one in `config.yaml` -- it is deployed with the Lambda. For Lambda, config is serialized to the `OPENCONTEXT_CONFIG` env var by Terraform.

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs ruff lint/format, pip-audit, pytest with coverage, and Go tests on push to main/develop and on PRs.
