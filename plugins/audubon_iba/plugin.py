"""Audubon Important Bird Areas (IBA) plugin for OpenContext.

This plugin fronts the National Audubon Society's public IBA feature
service -- a single, fixed ArcGIS FeatureServer with a known schema:

    .../iba_polygons_public/FeatureServer
        layer 0  IBA polygons (the main record)
        tables   criteria_site, criteria_species, habitat, landuse,
                 observation, ownership, site_description, species, threat

Because the service is fixed, there is no catalog to crawl and no dataset
id to resolve: every tool addresses layer 0 (or one of its related tables)
directly. What is carried over from the sibling ArcGIS forks is the REST
plumbing -- WGS84 in/out, coded-value domain decoding, pagination past the
server-side record cap, and error surfacing.

Coordinate contract: the polygons are authored in EPSG:3857 (Web
Mercator), but every query sets ``inSR=4326``/``outSR=4326``, so all tools
take and return WGS84 lat/lng and the service reprojects server-side.

Schema contract: layer 0's fields, coded-value domains, record cap, and
relationship ids are read from the service at runtime (``?f=json``) and
cached for the life of the instance -- nothing beyond the handful of
fields the tools actually name is hardcoded.
"""

import html
import logging
import math
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from core.interfaces import MCPPlugin, PluginType, ToolDefinition, ToolResult
from plugins.arcgis.where_validator import WhereValidator
from plugins.audubon_iba.config_schema import AudubonIBAPluginConfig

logger = logging.getLogger(__name__)

# ArcGIS descriptions and text fields carry HTML and smart punctuation;
# cleaning these keeps tool output readable and ASCII-safe.
_HTML_TAG_RE = re.compile(r"<[^>]+>")

# A balanced SQL string literal ('' is an escaped quote inside one).
_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")
_UNICODE_PUNCT = {
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "–": "-",
    "—": "--",
    "…": "...",
    " ": " ",
    "·": "-",
    "•": "-",
}

# Hard ceiling on pagination round-trips for any single tool call.
_MAX_QUERY_PAGES = 20

# Fallback page size, used only if the service does not report a cap.
_DEFAULT_MAX_RECORD_COUNT = 2000

# The service rejects buffers beyond a sane range long before this, but a
# 5000 km "radius" is a bug in the caller, not a query.
_MAX_RADIUS_KM = 500.0

# Public-facing category -> relationship name on layer 0. The numeric
# relationshipId that queryRelatedRecords actually wants is resolved from
# the live service metadata, not hardcoded: the ids are a property of the
# published service and would silently mis-route if they ever changed.
_CATEGORY_TO_RELATIONSHIP = {
    "species": "species",
    "criteria": "criteria_site",
    "criteria_site": "criteria_site",
    "criteria_species": "criteria_species",
    "habitat": "habitat",
    "landuse": "landuse",
    "ownership": "ownership",
    "threat": "threat",
    "observation": "observation",
    "description": "site_description",
    "site_description": "site_description",
}

# The nine categories actually advertised, and what each is good for. The
# map above additionally accepts `criteria_site` / `site_description` as
# aliases, but they resolve to the same tables as `criteria` / `description`
# -- advertising all four made an agent pick between byte-identical options.
# The enum comes from THIS dict, so every offered category has a purpose.
_CATEGORY_PURPOSE = {
    "species": "the birds the site was designated for",
    "criteria": "which IBA criteria the site meets (site-level)",
    "criteria_species": "which criteria each species triggers",
    "habitat": "habitat / ecosystem composition",
    "landuse": "how the land is used",
    "ownership": "who owns or stewards the land",
    "threat": "documented threats to the site",
    "observation": "historical survey and count records",
    "description": "narrative site description",
}

# Accepted-but-not-advertised aliases -> the category they really are.
_CANONICAL_CATEGORY = {
    "criteria_site": "criteria",
    "site_description": "description",
}

# Audubon's species table is frozen at a pre-2014 taxonomy, and its
# `species_code` is NOT eBird's `speciesCode` namespace -- though most codes
# coincide, which is exactly what makes this dangerous. Passing an Audubon
# code straight to eBird can query a DIFFERENT species and return a
# confident, correct-looking zero.
_TAXONOMY_WARNING = (
    "TAXONOMY WARNING -- do NOT pass `species_code` to eBird. These are "
    "Audubon's codes on a pre-2014 taxonomy, not eBird `speciesCode` values. "
    "Many coincide with eBird's, so a wrong one fails SILENTLY: eBird returns "
    "a plausible 'no records' for a species that is really present. Resolve "
    "each bird through eBird's taxonomy by `scientific_name` (or common name) "
    "first, then query."
)

# Taxa Audubon still files under a pre-split name, keyed by the Audubon
# species_code, with the successor to resolve to.
#
# Every code below was probed against the live eBird API on 2026-07-13, and
# they fall into two classes:
#
#   SILENT  -- the stale code is still VALID in eBird but denotes a different
#              (or effectively unreportable) taxon, so eBird answers "no
#              records" and the absence-of-evidence disclaimer makes the zero
#              read as real. These are the dangerous ones.
#   LOUD    -- the stale code is unknown to eBird, which rejects it outright.
#              Harmless (the caller cannot miss it), but naming the successor
#              saves a round-trip.
#
# eBird codes are quoted only where they were confirmed against the live API;
# elsewhere the successor is named as a species, to be resolved through eBird's
# taxonomy. This table is a curated hint, not an authority -- confirm before
# reporting, and note the right successor is often region-dependent.
_KNOWN_TAXONOMY_SPLITS = {
    # SILENT -- valid eBird code, wrong bird.
    "clarai": (
        "SILENT MISMATCH. Clapper Rail was split in 2014. 'clarai' is still a "
        "valid eBird code, but it now means Rallus crepitans -- an "
        "Atlantic/Gulf bird absent from the Pacific coast -- so eBird returns "
        "a believable 'no records' on western sites. There the bird is "
        "Ridgway's Rail (Rallus obsoletus, eBird 'ridrai1'); on Gulf/Atlantic "
        "sites 'clarai' is still correct."
    ),
    "commoo": (
        "SILENT MISMATCH. Common Moorhen was split in 2011. 'commoo' remains "
        "valid in eBird but denotes the Old World bird, so it returns a "
        "believable 'no records' anywhere in the Americas. The New World bird "
        "is Common Gallinule (Gallinula galeata, eBird 'comgal1')."
    ),
    "sagspa": (
        "SILENT MISMATCH. Sage Sparrow was split in 2013. 'sagspa' still "
        "resolves in eBird but yields no records; the living taxa are Bell's "
        "Sparrow (coastal/California) and Sagebrush Sparrow (interior). "
        "Resolve the right one for this site's region via eBird's taxonomy."
    ),
    "winwre": (
        "SILENT MISMATCH. Winter Wren was split in 2010. 'winwre' still "
        "resolves in eBird but yields no records; the living taxa are Winter "
        "Wren (eastern) and Pacific Wren (western). Resolve the right one for "
        "this site's region via eBird's taxonomy."
    ),
    "yelwar4": (
        "SILENT MISMATCH. 'yelwar4' resolves in eBird but returns no records "
        "anywhere -- it is not the species code for the bird Audubon means. "
        "Yellow-rumped Warbler is Setophaga coronata, eBird 'yerwar'."
    ),
    # LOUD -- eBird rejects the code outright; successor named to save a hop.
    "wesscr1": (
        "eBird rejects 'wesscr1'. Western Scrub-Jay was split in 2016 into "
        "California Scrub-Jay (Aphelocoma californica, eBird 'cowscj1') and "
        "Woodhouse's Scrub-Jay. Audubon's binomial for this record is the "
        "California bird."
    ),
    "whippo": (
        "eBird rejects 'whippo'. Whip-poor-will was split in 2010 into Eastern "
        "Whip-poor-will and Mexican Whip-poor-will; resolve via eBird's "
        "taxonomy."
    ),
    "blasco": (
        "eBird rejects 'blasco'. Black Scoter was split from Common Scoter; "
        "Audubon's binomial (Melanitta nigra) is now the Old World bird. The "
        "American bird is Black Scoter (Melanitta americana)."
    ),
    "santer": (
        "eBird rejects 'santer'. Sandwich Tern's New World population is "
        "treated as Cabot's Tern by some authorities; resolve via eBird's "
        "taxonomy."
    ),
}

# Acre -> square kilometre.
_SQ_KM_PER_ACRE = 0.00404685642

# Editor-tracking and geometry-bookkeeping columns. Real fields, but noise
# in a conversational answer -- suppressed from formatted output.
_NOISE_FIELDS = frozenset(
    {
        "created_user_et",
        "created_date_et",
        "last_edited_user_et",
        "last_edited_date_et",
        "Shape__Area",
        "Shape__Length",
        "globalid",
        "globalid_1",
        "guid",
    }
)

# Compact summary shown for list-style results (search / near-point).
_SUMMARY_FIELDS = (
    "site_id",
    "site_name",
    "state",
    "iba_priority",
    "iba_status",
    "flyway",
    "acres",
    "latitude",
    "longitude",
    "ebird_link",
)

# Called out in list_distinct_values as the fields worth enumerating.
_SUGGESTED_ENUM_FIELDS = ("flyway", "iba_priority", "iba_eba", "iba_status", "state")

_DESIGNATION_CAVEAT = (
    "IBA designation is a science-based conservation priority, not a legal "
    "or regulatory status: it does not protect land or restrict its use, and "
    "the boundaries are advisory rather than authoritative property lines."
)


class AudubonIBAPlugin(MCPPlugin):
    """Plugin for the National Audubon Society's public IBA feature service."""

    plugin_name = "audubon_iba"
    plugin_type = PluginType.OPEN_DATA
    plugin_version = "1.0.0"

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self.plugin_config: Optional[AudubonIBAPluginConfig] = None
        self.client: Optional[httpx.AsyncClient] = None
        # Layer/table metadata (fields, domains, record cap, relationships)
        # keyed by layer id, fetched on first use and cached for the life of
        # the instance. The service is fixed, so this never goes stale.
        self._meta_cache: Dict[int, Dict[str, Any]] = {}
        # Per-layer {field_name: decoder} maps, built once from the metadata.
        self._decoder_cache: Dict[int, Dict[str, Any]] = {}

    # ── Lifecycle ───────────────────────────────────────────────────────

    async def initialize(self) -> bool:
        try:
            self.plugin_config = AudubonIBAPluginConfig(**self.config)

            params = {}
            if self.plugin_config.token:
                params["token"] = self.plugin_config.token
            self.client = httpx.AsyncClient(
                headers={"Accept": "application/json"},
                params=params,
                timeout=self.plugin_config.timeout,
            )

            self._initialized = True
            logger.info(
                f"Audubon IBA plugin initialized against "
                f"{self.plugin_config.service_url} (layer "
                f"{self.plugin_config.layer_id})"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to initialize Audubon IBA plugin: {e}", exc_info=True)
            return False

    async def shutdown(self) -> None:
        if self.client:
            await self.client.aclose()
            self.client = None
        self._meta_cache.clear()
        self._decoder_cache.clear()
        self._initialized = False
        logger.info("Audubon IBA plugin shut down")

    async def health_check(self) -> bool:
        try:
            response = await self.client.get(self._layer_url, params={"f": "json"})
            return response.status_code == 200 and "error" not in response.json()
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            return False

    # ── Tool definitions ────────────────────────────────────────────────

    def get_tools(self) -> List[ToolDefinition]:
        # Advertise only the canonical nine; the aliases still resolve, they
        # just are not offered as choices.
        categories = sorted(_CATEGORY_PURPOSE)
        category_help = "; ".join(
            f"'{name}' = {purpose}" for name, purpose in _CATEGORY_PURPOSE.items()
        )
        return [
            ToolDefinition(
                name="find_ibas_near_point",
                description=(
                    "Find Important Bird Areas within a radius of a lat/lng "
                    "point -- 'which IBAs are near here?'. Buffers the point "
                    "and returns every IBA polygon the buffer intersects, "
                    "ordered by site name. Coordinates are WGS84 decimal "
                    "degrees. " + _DESIGNATION_CAVEAT
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "lat": {
                            "type": "number",
                            "description": "Latitude, WGS84 decimal degrees (-90 to 90).",
                            "minimum": -90,
                            "maximum": 90,
                        },
                        "lng": {
                            "type": "number",
                            "description": (
                                "Longitude, WGS84 decimal degrees (-180 to 180)."
                            ),
                            "minimum": -180,
                            "maximum": 180,
                        },
                        "radius_km": {
                            "type": "number",
                            "description": (
                                "Search radius in kilometres (default: 25, max: "
                                f"{int(_MAX_RADIUS_KM)})."
                            ),
                            "default": 25,
                            "exclusiveMinimum": 0,
                            "maximum": _MAX_RADIUS_KM,
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of IBAs (default: 25).",
                            "default": 25,
                            "minimum": 1,
                            "maximum": 200,
                        },
                    },
                    "required": ["lat", "lng"],
                },
            ),
            ToolDefinition(
                name="search_ibas",
                description=(
                    "Search Important Bird Areas by attribute: `state` (full "
                    "name or two-letter code), `priority` (Global, Continental, "
                    "or State), `flyway` (Pacific, Central, Mississippi, "
                    "Atlantic), and/or `name` (case-insensitive substring match "
                    "on the site name). Filters combine with AND; omit all of "
                    "them to browse. Output leads with TOTAL MATCHING, so for "
                    "'how many IBAs are in X?' you do not need to page through "
                    "results. Use list_distinct_values to confirm the exact "
                    "spelling of a code before filtering on it."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "state": {
                            "type": "string",
                            "description": (
                                "State name or two-letter abbreviation, e.g. "
                                "'Alaska' or 'AK'."
                            ),
                        },
                        "priority": {
                            "type": "string",
                            "description": (
                                "IBA priority rank: 'Global', 'Continental', or "
                                "'State'. This is the conservation significance "
                                "tier, not a legal status."
                            ),
                        },
                        "flyway": {
                            "type": "string",
                            "description": (
                                "Migratory flyway, e.g. 'Pacific', 'Central', "
                                "'Mississippi', 'Atlantic'."
                            ),
                        },
                        "name": {
                            "type": "string",
                            "description": (
                                "Case-insensitive substring of the site name, "
                                "e.g. 'coastal'."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of results (default: 25).",
                            "default": 25,
                            "minimum": 1,
                            "maximum": 200,
                        },
                    },
                    "required": [],
                },
            ),
            ToolDefinition(
                name="get_iba_details",
                description=(
                    "Get the full polygon record for one IBA by site_id -- "
                    "name, state, priority rank, status, flyway, area, "
                    "elevation, chapter contacts. Start here for any question "
                    "about a specific site, then pull related tables with "
                    "get_iba_related_records only if the question needs them. "
                    "Also returns the centroid and a site-sized "
                    "`suggested_radius_km`, which together are the ONLY working "
                    "bridge to eBird: eBird cannot address an IBA by id. "
                    "`ebird_link` is a human-facing web URL, NOT an API "
                    "identifier -- do not pass it, or the 'US-CA_202' inside "
                    "it, to any eBird tool."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "site_id": {
                            "type": "number",
                            "description": (
                                "IBA site_id, e.g. 1004 (Anchorage Coastal). "
                                "Get one from search_ibas or "
                                "find_ibas_near_point."
                            ),
                        },
                    },
                    "required": ["site_id"],
                },
            ),
            ToolDefinition(
                name="get_iba_related_records",
                description=(
                    "Get the related records for one IBA -- the tables behind "
                    "the polygon. Pick the category the question actually "
                    f"needs: {category_help}. Fetch the polygon with "
                    "get_iba_details first; only pull a related table when the "
                    "question calls for it. For recent or live bird sightings, "
                    "use an eBird tool at the site's centroid over the "
                    "'observation' table, whose records are historical. "
                    + _TAXONOMY_WARNING
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "site_id": {
                            "type": "number",
                            "description": "IBA site_id, e.g. 1004.",
                        },
                        "category": {
                            "type": "string",
                            "description": "Which related table to pull.",
                            "enum": categories,
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of records (default: 100).",
                            "default": 100,
                            "minimum": 1,
                            "maximum": 1000,
                        },
                    },
                    "required": ["site_id", "category"],
                },
            ),
            ToolDefinition(
                name="get_iba_boundary",
                description=(
                    "Get one IBA's actual polygon boundary (WGS84 rings) plus "
                    "its bounding box. Use this when 'inside the IBA' has to "
                    "mean inside, not near: IBAs are estuaries, river channels "
                    "and ridgelines, so a radius around the centroid both "
                    "over-includes neighbouring land and misses the far ends of "
                    "the site. Test eBird hotspot coordinates against these "
                    "rings for a true in-boundary audit. Boundaries are "
                    "advisory, not authoritative property lines."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "site_id": {
                            "type": "number",
                            "description": "IBA site_id, e.g. 202.",
                        },
                        "simplify": {
                            "type": "boolean",
                            "description": (
                                "Simplify the outline server-side (default: "
                                "true). Keeps the vertex count manageable; set "
                                "false for full precision."
                            ),
                            "default": True,
                        },
                    },
                    "required": ["site_id"],
                },
            ),
            ToolDefinition(
                name="list_distinct_values",
                description=(
                    "List the distinct values in one field of the IBA polygon "
                    "layer -- cheap enum discovery, so you can filter "
                    "search_ibas on an exact value instead of guessing. Most "
                    "useful for: " + ", ".join(_SUGGESTED_ENUM_FIELDS) + "."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "field": {
                            "type": "string",
                            "description": (
                                "Field name (CASE-SENSITIVE), e.g. 'flyway', "
                                "'iba_priority', 'iba_eba', 'iba_status'."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum distinct values (default: 200).",
                            "default": 200,
                            "minimum": 1,
                            "maximum": 1000,
                        },
                    },
                    "required": ["field"],
                },
            ),
        ]

    # ── Tool dispatch ───────────────────────────────────────────────────

    async def execute_tool(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> ToolResult:
        try:
            if tool_name == "find_ibas_near_point":
                text = await self._tool_find_near_point(arguments)
            elif tool_name == "search_ibas":
                text = await self._tool_search(arguments)
            elif tool_name == "get_iba_details":
                text = await self._tool_details(arguments)
            elif tool_name == "get_iba_related_records":
                text = await self._tool_related(arguments)
            elif tool_name == "get_iba_boundary":
                text = await self._tool_boundary(arguments)
            elif tool_name == "list_distinct_values":
                text = await self._tool_distinct(arguments)
            else:
                return ToolResult(
                    content=[],
                    success=False,
                    error_message=f"Unknown tool: {tool_name}",
                )

            return ToolResult(content=[{"type": "text", "text": text}], success=True)

        except Exception as e:
            logger.error(f"Error executing tool {tool_name}: {e}", exc_info=True)
            return ToolResult(
                content=[],
                success=False,
                error_message=str(e) if str(e) else "Tool execution failed",
            )

    # ── Tool implementations ────────────────────────────────────────────

    async def _tool_find_near_point(self, args: Dict[str, Any]) -> str:
        lat = args.get("lat")
        lng = args.get("lng")
        if lat is None or lng is None:
            raise ValueError("Both `lat` and `lng` are required.")
        radius_km = args.get("radius_km", 25)
        limit = args.get("limit", 25)

        records = await self.find_ibas_near_point(lat, lng, radius_km, limit)
        header = f"IBAs within {_fmt_number(radius_km)} km of ({lat}, {lng}):\n"
        if not records:
            return (
                header
                + "\nNo Important Bird Areas intersect that radius. Try a larger "
                "`radius_km`, or search by state with search_ibas."
            )
        body = await self._format_summaries(records, limit)
        return header + "\n" + body + "\n" + self._advisory_footer()

    async def _tool_search(self, args: Dict[str, Any]) -> str:
        where, described = self._build_search_where(args)
        limit = args.get("limit", 25)

        records = await self._query_layer_paged(
            self._layer_url,
            {
                "where": where,
                "outFields": ",".join(_SUMMARY_FIELDS),
                "orderByFields": "site_name",
            },
            limit,
        )
        total = await self._count(where)

        scope = f" matching {described}" if described else ""
        header = f"TOTAL MATCHING: {total} IBA(s){scope}\n"
        if not records:
            return (
                header + "\nNo IBAs matched. Check the exact spelling of a filter "
                "value with list_distinct_values."
            )
        body = await self._format_summaries(records, limit)
        return header + "\n" + body + "\n" + self._advisory_footer()

    async def _tool_details(self, args: Dict[str, Any]) -> str:
        site_id = _validate_site_id(args.get("site_id"))
        record = await self.get_iba_details(site_id)
        if record is None:
            return (
                f"No IBA found with site_id {_fmt_number(site_id)}. Find a valid "
                f"site_id with search_ibas or find_ibas_near_point."
            )
        return await self._format_details(record)

    async def _tool_related(self, args: Dict[str, Any]) -> str:
        site_id = _validate_site_id(args.get("site_id"))
        category = (args.get("category") or "").strip().lower()
        if not category:
            raise ValueError("`category` is required.")
        if category not in _CATEGORY_TO_RELATIONSHIP:
            raise ValueError(
                f"Unknown category {category!r}. Valid categories: "
                f"{', '.join(sorted(_CATEGORY_TO_RELATIONSHIP))}."
            )
        limit = args.get("limit", 100)

        site_name, records, table_id = await self.get_iba_related_records(
            site_id, category, limit
        )
        # An alias ('criteria_site') still resolves, but describe it as the
        # canonical category it actually is.
        category = _CANONICAL_CATEGORY.get(category, category)
        label = _CATEGORY_PURPOSE.get(category, category)
        header = (
            f"{category} records for {site_name} (site_id "
            f"{_fmt_number(site_id)}) -- {label}:\n"
        )
        if not records:
            return (
                header + f"\nNo {category} records are recorded for this site. Absence "
                "of a record means it was not entered, not that the attribute "
                "does not apply."
            )

        lines = [header, f"Returned {len(records)} record(s) (limit: {limit}):", ""]
        lines.append(await self._format_records(records, table_id))

        if category == "species":
            lines.append(_TAXONOMY_WARNING)
            splits = _split_notes(records)
            if splits:
                lines.append("")
                lines.append(
                    "Known taxonomy changes affecting this site's designated "
                    "species (confirm against eBird's live taxonomy):"
                )
                lines.extend(f"  - {note}" for note in splits)
        if category == "observation":
            lines.append(
                "Note: these are historical survey records. For recent or live "
                "sightings, query an eBird tool at the site's centroid "
                "(get_iba_details)."
            )
        return "\n".join(lines)

    async def _tool_boundary(self, args: Dict[str, Any]) -> str:
        site_id = _validate_site_id(args.get("site_id"))
        simplify = args.get("simplify", True)

        result = await self.get_iba_boundary(site_id, simplify=bool(simplify))
        if result is None:
            return (
                f"No IBA found with site_id {_fmt_number(site_id)}. Find a valid "
                f"site_id with search_ibas or find_ibas_near_point."
            )
        name, rings, bbox = result
        if not rings:
            return (
                f"{name} (site_id {_fmt_number(site_id)}) has no polygon geometry "
                f"in the service."
            )

        vertices = sum(len(r) for r in rings)
        lines = [
            f"Boundary of {name} (site_id {_fmt_number(site_id)}):",
            "",
            "Bounding box (WGS84): "
            f"west {bbox[0]:.6f}, south {bbox[1]:.6f}, "
            f"east {bbox[2]:.6f}, north {bbox[3]:.6f}",
            f"{len(rings)} ring(s), {vertices} vertices"
            + (" (simplified)" if simplify else " (full precision)"),
            "",
            "Rings as [lng, lat] pairs, WGS84. A point is inside the IBA if it "
            "falls inside an outer ring and outside any hole:",
        ]
        for i, ring in enumerate(rings, 1):
            coords = ", ".join(f"[{x:.6f}, {y:.6f}]" for x, y in ring)
            lines.append(f"  Ring {i} ({len(ring)} vertices): [{coords}]")
        lines.append("")
        lines.append(
            "To audit eBird coverage of this site: fetch hotspots over the "
            "bounding box (get_nearby_hotspots at the centroid with a radius "
            "that covers it), then keep only those whose coordinates fall "
            "inside these rings. A centroid radius alone is NOT the boundary."
        )
        lines.append("")
        lines.append(self._advisory_footer())
        return "\n".join(lines)

    async def _tool_distinct(self, args: Dict[str, Any]) -> str:
        field = args.get("field")
        if not field:
            raise ValueError("`field` is required.")
        limit = args.get("limit", 200)

        values = await self.list_distinct_values(field, limit)
        if not values:
            return f"No distinct values found for '{field}'."

        lines = [f"{len(values)} distinct value(s) for '{field}':", ""]
        for v in values:
            lines.append(f"  {v}")
        return "\n".join(lines)

    # ── Data access ─────────────────────────────────────────────────────

    async def find_ibas_near_point(
        self,
        lat: float,
        lng: float,
        radius_km: float = 25,
        limit: int = 25,
    ) -> List[Dict[str, Any]]:
        """IBA polygons intersecting a buffer around a WGS84 point.

        The point is sent as WGS84 (``inSR=4326``) with a server-side
        buffer in kilometres; the service reprojects into the layer's
        EPSG:3857. Passing the point without ``inSR`` would have it read
        as Web Mercator metres and silently match nothing.
        """
        if not isinstance(lat, (int, float)) or isinstance(lat, bool):
            raise ValueError(f"lat must be a number (got {lat!r})")
        if not isinstance(lng, (int, float)) or isinstance(lng, bool):
            raise ValueError(f"lng must be a number (got {lng!r})")
        if not -90 <= lat <= 90:
            raise ValueError(f"lat must be between -90 and 90 (got {lat})")
        if not -180 <= lng <= 180:
            raise ValueError(f"lng must be between -180 and 180 (got {lng})")
        if not isinstance(radius_km, (int, float)) or isinstance(radius_km, bool):
            raise ValueError(f"radius_km must be a number (got {radius_km!r})")
        if not 0 < radius_km <= _MAX_RADIUS_KM:
            raise ValueError(
                f"radius_km must be greater than 0 and at most "
                f"{_fmt_number(_MAX_RADIUS_KM)} (got {radius_km})"
            )

        return await self._query_layer_paged(
            self._layer_url,
            {
                "geometry": f"{lng},{lat}",
                "geometryType": "esriGeometryPoint",
                "distance": radius_km,
                "units": "esriSRUnit_Kilometer",
                "spatialRel": "esriSpatialRelIntersects",
                "where": "1=1",
                "outFields": ",".join(_SUMMARY_FIELDS),
                "orderByFields": "site_name",
            },
            limit,
        )

    async def get_iba_details(self, site_id: float) -> Optional[Dict[str, Any]]:
        """The full layer-0 record for one site_id, or None if absent."""
        where = f"site_id = {_fmt_number(site_id)}"
        records = await self._query_layer_paged(
            self._layer_url, {"where": where, "outFields": "*"}, limit=1
        )
        return records[0] if records else None

    async def get_iba_related_records(
        self, site_id: float, category: str, limit: int = 100
    ) -> Tuple[str, List[Dict[str, Any]], int]:
        """Related records for a site, via the service's queryRelatedRecords.

        Returns ``(site_name, records, related_table_id)``. queryRelatedRecords
        keys on the origin layer's objectid, not site_id, so the site is
        resolved first -- which doubles as the existence check.
        """
        relationship_name = _CATEGORY_TO_RELATIONSHIP[category]
        object_id, site_name = await self._resolve_object_id(site_id)
        relationship = await self._relationship(relationship_name)
        relationship_id = int(relationship["id"])
        related_table_id = int(relationship["relatedTableId"])

        records: List[Dict[str, Any]] = []
        offset = 0
        page_size = await self._max_record_count(related_table_id)
        for _ in range(_MAX_QUERY_PAGES):
            want = min(page_size, limit - len(records))
            params = {
                "objectIds": object_id,
                "relationshipId": relationship_id,
                "outFields": "*",
                "returnGeometry": "false",
                "resultRecordCount": want,
                "f": "json",
            }
            if offset:
                params["resultOffset"] = offset

            data = await self._get_json(
                f"{self._layer_url}/queryRelatedRecords", params
            )
            page = [
                rec.get("attributes", {})
                for group in data.get("relatedRecordGroups", []) or []
                for rec in group.get("relatedRecords", []) or []
            ]
            records.extend(page)

            if len(records) >= limit or len(page) < want:
                break
            offset += len(page)

        return site_name, records[:limit], related_table_id

    async def get_iba_boundary(
        self, site_id: float, simplify: bool = True
    ) -> Optional[Tuple[str, List[List[Tuple[float, float]]], Tuple[float, ...]]]:
        """The WGS84 polygon rings and bbox for one site, or None if absent.

        The shared pager sends ``returnGeometry=false`` (every other tool wants
        attributes only), so geometry gets its own request. ``maxAllowableOffset``
        simplifies server-side -- an IBA can carry thousands of vertices, and the
        Douglas-Peucker tolerance is in the OUTPUT SR's units, i.e. degrees here.
        """
        params = {
            "where": f"site_id = {_fmt_number(site_id)}",
            "outFields": "site_name",
            "returnGeometry": "true",
            "geometryPrecision": 6,
            "inSR": 4326,
            "outSR": 4326,
            "resultRecordCount": 1,
            "f": "json",
        }
        if simplify:
            # ~0.0001 deg =~ 11 m: well inside the service's own "Good" (+/- 3)
            # boundary accuracy, so this discards noise, not signal.
            params["maxAllowableOffset"] = 0.0001

        data = await self._get_json(f"{self._layer_url}/query", params)
        features = data.get("features") or []
        if not features:
            return None

        feature = features[0]
        name = (feature.get("attributes") or {}).get("site_name") or "(unnamed site)"
        raw_rings = (feature.get("geometry") or {}).get("rings") or []

        rings: List[List[Tuple[float, float]]] = []
        xs: List[float] = []
        ys: List[float] = []
        for raw_ring in raw_rings:
            ring = [
                (float(pt[0]), float(pt[1]))
                for pt in raw_ring
                if isinstance(pt, (list, tuple)) and len(pt) >= 2
            ]
            if ring:
                rings.append(ring)
                xs.extend(x for x, _ in ring)
                ys.extend(y for _, y in ring)

        bbox = (min(xs), min(ys), max(xs), max(ys)) if xs else (0.0, 0.0, 0.0, 0.0)
        return name, rings, bbox

    async def list_distinct_values(self, field: str, limit: int = 200) -> List[Any]:
        """Distinct values of one layer-0 field."""
        field = await self._validate_field(field)
        data = await self._get_json(
            f"{self._layer_url}/query",
            {
                "where": "1=1",
                "outFields": field,
                "returnDistinctValues": "true",
                "returnGeometry": "false",
                "orderByFields": field,
                "resultRecordCount": min(max(int(limit), 1), 1000),
                "inSR": 4326,
                "outSR": 4326,
                "f": "json",
            },
        )
        decode = (await self._decoders())[field]
        values = []
        for feature in data.get("features", []):
            attrs = feature.get("attributes", {})
            if field in attrs and attrs[field] is not None:
                values.append(decode(attrs[field]))
        return values

    # ── ArcGIS REST plumbing ────────────────────────────────────────────

    @property
    def _layer_url(self) -> str:
        return f"{self.plugin_config.service_url}/{self.plugin_config.layer_id}"

    async def _get_json(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """GET an ArcGIS REST endpoint, raising on HTTP or in-body errors.

        ArcGIS reports failures with HTTP 200 and an ``error`` object in the
        body, so checking the status code alone is not enough.
        """
        try:
            response = await self.client.get(url, params=params)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"IBA service error (HTTP {e.response.status_code}): "
                f"{e.response.text[:500]}"
            ) from e
        except httpx.RequestError as e:
            raise RuntimeError(
                f"Could not reach the IBA service ({type(e).__name__}): {e}"
            ) from e

        try:
            data = response.json()
        except Exception as json_err:
            content_type = response.headers.get("content-type", "")
            raise ValueError(
                f"The IBA service returned a non-JSON response (content-type: "
                f"{content_type})."
            ) from json_err

        err = data.get("error")
        if err:
            code = err.get("code", "unknown")
            message = err.get("message", "Unknown error")
            details = "; ".join(err.get("details", []) or [])
            hint = ""
            if code in (401, 403, 498, 499) or "token" in str(message).lower():
                hint = (
                    " -- the public IBA service should not require a token; "
                    "check the configured service_url."
                )
            raise RuntimeError(
                f"IBA query failed (code {code}): {message}"
                + (f" -- {details}" if details else "")
                + hint
            )
        return data

    async def _query_layer_paged(
        self, layer_url: str, params: Dict[str, Any], limit: int
    ) -> List[Dict[str, Any]]:
        """Run a layer /query, paging past the service's record cap.

        The service caps a single response at maxRecordCount (2000 for this
        layer), so anything larger is fetched with resultOffset.
        """
        if limit < 1:
            raise ValueError(f"limit must be at least 1 (got {limit})")

        where = _validate_where(params.get("where", "1=1"))
        page_size = await self._max_record_count()

        records: List[Dict[str, Any]] = []
        offset = 0
        for _ in range(_MAX_QUERY_PAGES):
            want = min(page_size, limit - len(records))
            page_params = {
                **params,
                "where": where,
                "resultRecordCount": want,
                # WGS84 in and out; the layer is EPSG:3857 and the service
                # reprojects. Without inSR, a WGS84 point would be read as
                # Web Mercator metres and match nothing.
                "inSR": 4326,
                "outSR": 4326,
                "returnGeometry": "false",
                "f": "json",
            }
            if offset:
                page_params["resultOffset"] = offset

            data = await self._get_json(f"{layer_url}/query", page_params)
            features = data.get("features", [])
            records.extend(f.get("attributes", {}) for f in features)

            if len(records) >= limit or len(features) < want:
                break
            offset += len(features)

        return records[:limit]

    async def _count(self, where: str) -> int:
        """Total records matching a WHERE clause (returnCountOnly)."""
        data = await self._get_json(
            f"{self._layer_url}/query",
            {
                "where": _validate_where(where),
                "returnCountOnly": "true",
                "f": "json",
            },
        )
        return int(data.get("count", 0))

    async def _resolve_object_id(self, site_id: float) -> Tuple[int, str]:
        """Map a site_id to its objectid (and site_name) on layer 0."""
        records = await self._query_layer_paged(
            self._layer_url,
            {
                "where": f"site_id = {_fmt_number(site_id)}",
                "outFields": "objectid,site_name",
            },
            limit=1,
        )
        if not records:
            raise ValueError(
                f"No IBA found with site_id {_fmt_number(site_id)}. Find a valid "
                f"site_id with search_ibas or find_ibas_near_point."
            )
        record = records[0]
        return int(record["objectid"]), record.get("site_name") or "(unnamed site)"

    # ── Runtime schema introspection ────────────────────────────────────

    async def _metadata(self, layer_id: Optional[int] = None) -> Dict[str, Any]:
        """Live metadata for a layer or related table, fetched once and cached."""
        layer_id = self._resolve_layer_id(layer_id)
        if layer_id not in self._meta_cache:
            self._meta_cache[layer_id] = await self._get_json(
                f"{self.plugin_config.service_url}/{layer_id}", {"f": "json"}
            )
        return self._meta_cache[layer_id]

    def _resolve_layer_id(self, layer_id: Optional[int]) -> int:
        return self.plugin_config.layer_id if layer_id is None else int(layer_id)

    async def _max_record_count(self, layer_id: Optional[int] = None) -> int:
        meta = await self._metadata(layer_id)
        return int(meta.get("maxRecordCount") or _DEFAULT_MAX_RECORD_COUNT)

    async def _fields(
        self, layer_id: Optional[int] = None
    ) -> Dict[str, Dict[str, Any]]:
        meta = await self._metadata(layer_id)
        return {f["name"]: f for f in meta.get("fields", []) or [] if f.get("name")}

    async def _validate_field(self, field: str) -> str:
        """Confirm a caller-supplied field name is real before interpolating it.

        The field name lands in ``outFields``/``orderByFields``, so this is a
        security boundary as well as a typo guard.
        """
        fields = await self._fields()
        if field in fields:
            return field
        raise ValueError(
            f"Field {field!r} does not exist on the IBA layer (field names are "
            f"CASE-SENSITIVE). Common fields: "
            f"{', '.join(_SUGGESTED_ENUM_FIELDS)}. Available: "
            f"{', '.join(sorted(fields))}"
        )

    async def _relationship(self, relationship_name: str) -> Dict[str, Any]:
        """Resolve a relationship by name to its live definition.

        queryRelatedRecords takes the ``relationshipId``, which is NOT the
        related table's id -- resolving it from the service rather than
        hardcoding it means a republished service cannot silently re-point a
        category at the wrong table.
        """
        meta = await self._metadata()
        for rel in meta.get("relationships", []) or []:
            if rel.get("name") == relationship_name:
                return rel
        available = sorted(
            r.get("name", "") for r in meta.get("relationships", []) or []
        )
        raise RuntimeError(
            f"The IBA service does not expose a {relationship_name!r} "
            f"relationship. Available: {', '.join(available)}"
        )

    async def _decoders(self, layer_id: Optional[int] = None) -> Dict[str, Any]:
        """{field_name: decoder} for a layer, built once from its metadata.

        Coded-value domains store a code that may differ from the label, and
        date fields come back as epoch milliseconds. Decoding both means the
        model never has to interpret raw storage values. Related tables have
        their own schemas, so decoders are resolved per table -- decoding a
        species record against layer 0's fields would leave its dates raw.
        """
        layer_id = self._resolve_layer_id(layer_id)
        if layer_id in self._decoder_cache:
            return self._decoder_cache[layer_id]

        decoders = {}
        for name, field in (await self._fields(layer_id)).items():
            decoders[name] = self._build_decoder(field)
        self._decoder_cache[layer_id] = decoders
        return decoders

    def _build_decoder(self, field: Dict[str, Any]):
        field_type = field.get("type", "")
        domain = field.get("domain") or {}
        coded = domain.get("codedValues") if isinstance(domain, dict) else None
        lookup = {c.get("code"): c.get("name") for c in coded} if coded else {}

        def decode(value: Any) -> Any:
            if value is None:
                return None
            if lookup and value in lookup:
                return lookup[value]
            if field_type == "esriFieldTypeDate" and isinstance(value, (int, float)):
                return _epoch_ms_to_iso(value)
            if isinstance(value, str):
                return self._clean_text(value)
            return value

        return decode

    async def _decode_record(
        self, record: Dict[str, Any], layer_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """Decode every value in a record through its field's decoder."""
        decoders = await self._decoders(layer_id)
        return {
            key: (decoders[key](value) if key in decoders else value)
            for key, value in record.items()
        }

    # ── WHERE construction ──────────────────────────────────────────────

    def _build_search_where(self, args: Dict[str, Any]) -> Tuple[str, str]:
        """Compose the search WHERE clause from the optional filters.

        Callers never supply raw SQL -- each filter is a value that is quote-
        escaped and dropped into a fixed predicate -- but the composed clause
        still goes through WhereValidator as defence in depth.
        """
        clauses: List[str] = []
        described: List[str] = []

        state = _clean_arg(args.get("state"))
        if state:
            value = _sql_quote(state)
            # The layer carries both the full name ('Alaska') and the
            # abbreviation ('AK'); accept either without making the caller
            # know which.
            clauses.append(
                f"(UPPER(state) = UPPER({value}) OR UPPER(state_abbr) = UPPER({value}))"
            )
            described.append(f"state={state}")

        priority = _clean_arg(args.get("priority"))
        if priority:
            clauses.append(f"UPPER(iba_priority) = UPPER({_sql_quote(priority)})")
            described.append(f"priority={priority}")

        flyway = _clean_arg(args.get("flyway"))
        if flyway:
            clauses.append(f"UPPER(flyway) = UPPER({_sql_quote(flyway)})")
            described.append(f"flyway={flyway}")

        name = _clean_arg(args.get("name"))
        if name:
            pattern = _sql_quote(f"%{_escape_like(name)}%")
            clauses.append(f"UPPER(site_name) LIKE UPPER({pattern}) ESCAPE '\\'")
            described.append(f"name~'{name}'")

        where = " AND ".join(clauses) if clauses else "1=1"
        return _validate_where(where), ", ".join(described)

    # ── Output formatting ───────────────────────────────────────────────

    def _advisory_footer(self) -> str:
        org = self.plugin_config.organization if self.plugin_config else "Audubon"
        return (
            f"---\n{_DESIGNATION_CAVEAT}\nSource: {org} public IBA service. "
            "Formal reuse of this spatial data goes through Audubon's data "
            "request process."
        )

    async def _format_summaries(self, records: List[Dict[str, Any]], limit: int) -> str:
        lines = [f"Returned {len(records)} IBA(s) (limit: {limit}):", ""]
        for i, raw in enumerate(records, 1):
            record = await self._decode_record(raw)
            name = record.get("site_name") or "(unnamed site)"
            lines.append(f"{i}. {name}")
            lines.append(f"   site_id: {_fmt_number(record.get('site_id'))}")

            facts = []
            if record.get("iba_priority"):
                facts.append(f"priority: {record['iba_priority']}")
            if record.get("iba_status"):
                facts.append(f"status: {record['iba_status']}")
            if record.get("state"):
                facts.append(f"state: {record['state']}")
            if record.get("flyway"):
                facts.append(f"flyway: {record['flyway']}")
            if record.get("acres") is not None:
                facts.append(f"acres: {_fmt_number(record['acres'])}")
            if facts:
                lines.append(f"   {' | '.join(facts)}")

            if (
                record.get("latitude") is not None
                and record.get("longitude") is not None
            ):
                lines.append(
                    f"   center: {_fmt_number(record['latitude'])}, "
                    f"{_fmt_number(record['longitude'])}"
                )
            if record.get("ebird_link"):
                lines.append(f"   eBird: {record['ebird_link']}")
            lines.append("")
        return "\n".join(lines)

    async def _format_details(self, raw: Dict[str, Any]) -> str:
        record = await self._decode_record(raw)
        name = record.get("site_name") or "(unnamed site)"

        lines = [
            f"IBA: {name}",
            f"site_id: {_fmt_number(record.get('site_id'))}",
            "",
        ]

        # The centroid + a site-sized radius is the only bridge to eBird that
        # actually works: eBird has no notion of an IBA id, and the ebird_link
        # below is a human web URL. Spell the chain out so an agent does not
        # have to infer it -- and does not fall back to eBird's 25 km default,
        # which is metro-scale against a site of a few square kilometres.
        lat = record.get("latitude")
        lng = record.get("longitude")
        radius_km = _suggested_radius_km(record.get("acres"))
        if lat is not None and lng is not None:
            lines.append("eBird bridge (centroid -- IBAs have no eBird id):")
            lines.append(f"  lat: {_fmt_number(lat)}, lng: {_fmt_number(lng)}")
            if radius_km:
                lines.append(
                    f"  suggested_radius_km: {_fmt_number(radius_km)} "
                    f"(equal-area circle for this site; eBird's 25 km default "
                    f"would scan a whole metro)"
                )
            lines.append(
                "  Chain: get_nearby_hotspots(lat, lng, dist=suggested_radius_km) "
                "-> get_recent_observations(locId) per hotspot. For a true "
                "in-boundary audit rather than a radius, use get_iba_boundary "
                "and filter hotspots to the polygon."
            )
            lines.append("")

        if record.get("ebird_link"):
            lines.append(f"eBird link (for humans): {record['ebird_link']}")
            lines.append(
                "  (Opens this IBA in eBird's web Explore tool. It is NOT an "
                "API identifier -- the 'US-CA_202'-style id inside it is an "
                "Audubon IBA code, not an eBird regionCode or hotspot locId, "
                "and eBird tools will reject it. Use the centroid above.)"
            )
            lines.append("")

        lines.append("Full record:")
        for key, value in record.items():
            if key in _NOISE_FIELDS or key in ("site_name", "site_id", "ebird_link"):
                continue
            if value is None or value == "":
                continue
            lines.append(f"  {key}: {value}")

        lines.append("")
        lines.append(
            "Related tables available via get_iba_related_records: "
            + ", ".join(_CATEGORY_PURPOSE)
        )
        lines.append("")
        lines.append(self._advisory_footer())
        return "\n".join(lines)

    async def _format_records(
        self, records: List[Dict[str, Any]], table_id: int
    ) -> str:
        lines = []
        for i, raw in enumerate(records, 1):
            record = await self._decode_record(raw, table_id)
            lines.append(f"Record {i}:")

            # `sci_name` holds only the species epithet ('longirostris'); the
            # genus lives in `iba_genus`. Read alone it is half a name, so
            # compose the binomial rather than leaving every consumer to
            # rediscover the split. Raw fields stay for provenance.
            binomial = _compose_binomial(record)
            if binomial:
                lines.append(f"  scientific_name: {binomial}")

            for key, value in record.items():
                if key in _NOISE_FIELDS:
                    continue
                if value is None or value == "":
                    continue
                lines.append(f"  {key}: {value}")
            lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _clean_text(value: Any) -> str:
        """Strip HTML and normalize to readable ASCII."""
        if value is None:
            return ""
        text = html.unescape(str(value))
        text = _HTML_TAG_RE.sub(" ", text)
        for uni, ascii_ in _UNICODE_PUNCT.items():
            text = text.replace(uni, ascii_)
        text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
        return re.sub(r"\s+", " ", text).strip()


# ── Module-level helpers ────────────────────────────────────────────────


def _compose_binomial(record: Dict[str, Any]) -> str:
    """Rebuild 'Genus epithet' from a species record's split-apart fields.

    Returns '' unless both halves are present. Note the result may still be
    STALE taxonomy (Rallus longirostris was split into obsoletus/crepitans),
    so it is a lookup key for eBird's taxonomy, not an authority.
    """
    genus = str(record.get("iba_genus") or "").strip()
    epithet = str(record.get("sci_name") or "").strip()
    if not genus or not epithet:
        return ""
    return f"{genus} {epithet}"


def _split_notes(records: List[Dict[str, Any]]) -> List[str]:
    """Curated split/lump notes for the species codes present in a result set."""
    notes = []
    for record in records:
        code = str(record.get("species_code") or "").strip().lower()
        note = _KNOWN_TAXONOMY_SPLITS.get(code)
        if note:
            name = record.get("common_name") or code
            entry = f"{name} (species_code '{code}'): {note}"
            if entry not in notes:
                notes.append(entry)
    return notes


def _suggested_radius_km(acres: Any) -> Optional[float]:
    """Radius of the circle with the same area as the site, in km.

    An IBA is a few square kilometres; eBird's nearby tools default to a 25 km
    radius, which for a site like Mission Bay over-scans by ~160x and drags in
    inland hotspots that are nowhere near it. This gives an agent a site-sized
    number instead of a guessed constant. Still a circle, so it is an
    approximation -- get_iba_boundary is the exact answer.
    """
    if acres is None or isinstance(acres, bool) or not isinstance(acres, (int, float)):
        return None
    if acres <= 0 or math.isnan(acres) or math.isinf(acres):
        return None
    radius = math.sqrt((acres * _SQ_KM_PER_ACRE) / math.pi)
    # eBird's nearby radius maxes out at 50 km; never suggest past it. Floor at
    # 1 km so a tiny site still returns a usable query.
    return round(min(max(radius, 1.0), 50.0), 1)


def _validate_where(where: str) -> str:
    """Run the shared WhereValidator with string literals blanked out.

    Every WHERE this plugin sends is composed here, with caller values
    quote- and wildcard-escaped into string literals. The shared validator
    scans literal content too, so real site names ('Union Slough', 'Grant
    Lake') would be rejected as injection. Blanking balanced literals scans
    only the SQL structure; breaking OUT of a literal takes an unbalanced
    quote, which leaves the injected tail unstripped and still scanned.
    """
    where = (where or "").strip() or "1=1"
    WhereValidator.validate(_STRING_LITERAL_RE.sub("''", where))
    return where


def _clean_arg(value: Any) -> str:
    """Normalize an optional string filter to a stripped str ('' if unset)."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"Expected a string filter value, got {value!r}")
    return value.strip()


def _sql_quote(value: str) -> str:
    """Quote a string literal for an ArcGIS WHERE clause."""
    return "'" + value.replace("'", "''") + "'"


def _escape_like(value: str) -> str:
    """Escape LIKE wildcards so a user's '%' matches a literal '%'."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _validate_site_id(value: Any) -> float:
    """Validate site_id, which is interpolated into a WHERE clause."""
    if value is None:
        raise ValueError("`site_id` is required.")
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"site_id must be a number (got {value!r})")
    try:
        site_id = float(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"site_id must be a number (got {value!r})") from e
    if math.isnan(site_id) or math.isinf(site_id):
        raise ValueError(f"site_id must be a finite number (got {value!r})")
    return site_id


def _fmt_number(value: Any) -> str:
    """Render a number without float noise or scientific notation.

    site_id is a Double in the schema but always integral in practice; a
    naive str() would render 1004.0 and produce a WHERE clause that reads
    oddly, while very large/small floats would go exponential and break the
    SQL entirely.
    """
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return str(value)
        if value.is_integer():
            return str(int(value))
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return str(value)


def _epoch_ms_to_iso(value: float) -> str:
    """ArcGIS dates are epoch milliseconds; render them as ISO dates."""
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return str(value)
