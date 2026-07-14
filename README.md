# OpenContext

<p align="center">
  <img src="docs/opencontext_logo.png" alt="OpenContext Logo" width="400">
</p>

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-green.svg)](https://modelcontextprotocol.io/)

---

**Audubon IBA MCP** — a National Audubon Society [Important Bird Areas](https://www.audubon.org/important-bird-areas) fork of OpenContext (forked from the San Diego City GIS fork). It serves Audubon's public IBA feature service through the `audubon_iba` plugin.

Where the sibling GIS forks front a whole portal or services directory, this one fronts **one fixed FeatureServer with a known schema**. There is nothing to crawl, so the catalog-discovery machinery is gone: no `search_datasets`, no `get_aggregations`, no catalog manifest, no dataset-id resolution. What carries over is the ArcGIS REST plumbing — WGS84 handling, coded-value domain decoding, pagination, and error surfacing.

> **An IBA is a conservation priority, not a legal status.**
> IBA designation is science-based (BirdLife/Audubon criteria, ranked Global / Continental / State). It carries **no legal or regulatory force**: it does not protect land, restrict its use, or trigger permitting, and the boundaries are advisory rather than authoritative property lines. This server states that in its MCP `instructions`, in its tool descriptions, and in a footer on its output.

---

## The data source

```
https://services1.arcgis.com/lDFzr3JyGEn5Eymu/arcgis/rest/services/iba_polygons_public/FeatureServer

  layer 0   iba_polygons_public  -- the main IBA polygon record
  tables    3 criteria_site    4 criteria_species   5 habitat   6 landuse
            7 observation      8 ownership          9 site_description
           10 species         11 threat
```

Public and key-less. Layer 0's fields, coded-value domains, record cap (2000), and relationship ids are read from the service **at runtime** (`?f=json`) and cached — nothing is hardcoded beyond the fields the tools actually name.

Sites are addressed by **`site_id`** (e.g. `1004` = Anchorage Coastal), which you get from `search_ibas` or `find_ibas_near_point`.

## The WGS84 contract

The polygons are authored in **EPSG:3857** (Web Mercator). Every query sets `inSR=4326` and `outSR=4326`, so all tools take and return WGS84 lat/lng and the service reprojects server-side. Without `inSR`, a WGS84 point would be read as Web Mercator metres and silently match nothing. `find_ibas_near_point` adds a server-side buffer (`distance` + `units=esriSRUnit_Kilometer`) rather than computing geometry client-side.

Pagination is metadata-driven: `maxRecordCount` is read from the service, and both `/query` and `/queryRelatedRecords` page with `resultOffset`/`resultRecordCount`.

## Tools exposed

| Tool | Purpose |
| ---- | ------- |
| `audubon_iba__find_ibas_near_point` | "Which IBAs are near here?" — buffers a WGS84 `lat`/`lng` by `radius_km` and returns every IBA polygon it intersects |
| `audubon_iba__search_ibas` | Attribute search: `state` (name **or** two-letter code), `priority` (Global/Continental/State), `flyway`, `name` (case-insensitive substring). Output leads with `TOTAL MATCHING`, so "how many IBAs in X?" needs no paging |
| `audubon_iba__get_iba_details` | The full polygon record for one `site_id`, leading with the centroid + `suggested_radius_km` that bridge to eBird |
| `audubon_iba__get_iba_related_records` | The tables behind a site, by `category`: `species`, `criteria`, `criteria_species`, `habitat`, `landuse`, `ownership`, `threat`, `observation`, `description` |
| `audubon_iba__get_iba_boundary` | The site's actual polygon (WGS84 rings + bbox), for when *inside the IBA* has to mean inside rather than near |
| `audubon_iba__list_distinct_values` | Cheap enum discovery for a field (`flyway`, `iba_priority`, `iba_eba`, `iba_status`, …) so you can filter on an exact value instead of guessing |

**Workflow:** fetch the polygon record first, then pull a related table only when the question needs one — `species`/`criteria` for *why it was designated*, `threat`/`ownership`/`landuse` for *stewardship context*, `habitat` for *ecosystem*, `observation` for *history*.

## Chaining to eBird — read this before you do

The obvious mashup ("which designated species are being seen here right now?") has two traps that return **confident, silently wrong answers**. Both are handled by the server, but only if you use it as intended:

- **Never pass `species_code` to eBird.** Audubon's taxonomy is frozen pre-2014, and *most* of its codes happen to coincide with eBird's — which is exactly what makes the rest dangerous. Site 202 is designated for "Clapper Rail"/`clarai`, but `clarai` is eBird's code for an Atlantic bird absent from California; the bird actually there is Ridgway's Rail (`ridrai1`). eBird answers "no records" and its absence-of-evidence disclaimer makes the zero look real. Resolve every bird through eBird's taxonomy by scientific name first.
- **`ebird_link` is not a join key.** It is a human-facing web URL; the `US-CA_202` inside it is an Audubon id that every eBird tool rejects. The working bridge is the **centroid** plus the `suggested_radius_km` that `get_iba_details` returns (eBird's 25 km default is metro-scale against a site of a few km²). For a true in-boundary audit rather than a radius, use `get_iba_boundary` and filter hotspot coordinates against the rings.

Full details, including the probed classification of every stale code: **[docs/EBIRD_COMPOSABILITY.md](docs/EBIRD_COMPOSABILITY.md)**.

## Connect to the server

Add it as a custom connector in Claude (same steps on Claude.ai and Claude Desktop):

1. **Settings → Connectors** (or **Customize → Connectors** on claude.ai)
2. **Add custom connector**
3. Name it e.g. `Audubon IBA` and paste your deployment's `/mcp` URL.

Quick health check from a terminal:

```bash
curl -sS -X POST http://localhost:8000/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"ping"}'
# → {"jsonrpc":"2.0","id":1,"result":{"status":"ok"}}
```

Raw JSON-RPC example:

```bash
curl -sS -X POST http://localhost:8000/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"audubon_iba__find_ibas_near_point",
                 "arguments":{"lat":61.17,"lng":-149.9,"radius_km":40}}}'
```

## Verified end-to-end

The definition-of-done is a two-step chain around **Anchorage** (61.17, -149.9): a 40 km buffer search, then a related-record traversal on a site it returns. Together they prove the WGS84 buffer contract and the `site_id → objectid → queryRelatedRecords` chain (the relationship id is *not* the related table's id, and `queryRelatedRecords` keys on `objectid`, not `site_id`).

```jsonc
// audubon_iba__find_ibas_near_point
{ "lat": 61.17, "lng": -149.9, "radius_km": 40 }
```

returns six Alaska IBAs — Anchorage Coastal, Campbell Creek, Goose Bay, Palmer Hay Flats, Susitna Flats, Swanson Lakes — each with its priority rank and `ebird_link`. Traversing one of them:

```jsonc
// audubon_iba__get_iba_related_records
{ "site_id": 1004, "category": "species" }
```

returns the birds Anchorage Coastal was designated for (Short-billed Dowitcher, Snow Goose, Hudsonian Godwit, Sandhill Crane).

`scripts/smoke_prod.py` runs both plus eleven more checks against any deployment — including that species records still carry the eBird taxonomy warning, and that a site whose name contains a SQL keyword (`Union`) is still searchable:

```bash
python scripts/smoke_prod.py                             # production
python scripts/smoke_prod.py http://localhost:8000/mcp   # local
```

## Data use

The endpoint is public, but Audubon routes formal reuse of the spatial data through a request process. Ad-hoc display and analysis in conversation is fine; anything beyond that should go through Audubon.

## Local development

```bash
uv sync                              # or: pip install -r requirements.txt
python scripts/local_server.py       # serves http://localhost:8000/mcp
python -m pytest tests/ -q           # tests
```

On Windows, `set PYTHONIOENCODING=utf-8` before `local_server.py` (it prints emoji).

See `CLAUDE.md` and `docs/` for architecture, deployment, and plugin development.
