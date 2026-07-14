"""Tests for the Audubon Important Bird Areas (IBA) plugin.

Cover the fixed-service contract: WGS84 in/out with a server-side buffer,
WHERE composition from the attribute filters (including quote/wildcard
escaping), site_id -> objectid resolution before queryRelatedRecords,
relationship-id resolution from live metadata, per-table coded-value and
date decoding, pagination past the record cap, and error surfacing.

No network access: HTTP is mocked with a fake service that routes on URL.
"""

import re
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from pydantic import ValidationError

from core.interfaces import PluginType
from plugins.audubon_iba.config_schema import AudubonIBAPluginConfig
from plugins.audubon_iba.plugin import (
    AudubonIBAPlugin,
    _compose_binomial,
    _escape_like,
    _fmt_number,
    _suggested_radius_km,
    _validate_site_id,
)

SERVICE_URL = (
    "https://services1.example.com/arcgis/rest/services/"
    "iba_polygons_public/FeatureServer"
)

# Layer 0: the IBA polygons. Mirrors the real schema's shape -- coded-value
# domains on iba_priority/iba_status, a date field, and the nine
# relationships (only the two exercised here are spelled out).
LAYER0_META = {
    "name": "iba_polygons_public",
    "geometryType": "esriGeometryPolygon",
    "maxRecordCount": 2,  # small, so pagination is exercised
    "objectIdField": "objectid",
    "fields": [
        {"name": "objectid", "type": "esriFieldTypeOID"},
        {"name": "site_id", "type": "esriFieldTypeDouble"},
        {"name": "site_name", "type": "esriFieldTypeString"},
        {"name": "state", "type": "esriFieldTypeString"},
        {"name": "state_abbr", "type": "esriFieldTypeString"},
        {"name": "flyway", "type": "esriFieldTypeString"},
        {"name": "acres", "type": "esriFieldTypeDouble"},
        {"name": "latitude", "type": "esriFieldTypeDouble"},
        {"name": "longitude", "type": "esriFieldTypeDouble"},
        {"name": "ebird_link", "type": "esriFieldTypeString"},
        {"name": "created_user_et", "type": "esriFieldTypeString"},
        {
            "name": "iba_priority",
            "type": "esriFieldTypeString",
            "domain": {
                "type": "codedValue",
                "codedValues": [
                    {"code": "G", "name": "Global"},
                    {"code": "Continental", "name": "Continental"},
                ],
            },
        },
        {
            "name": "iba_status",
            "type": "esriFieldTypeString",
            "domain": {
                "type": "codedValue",
                "codedValues": [{"code": "Recognized", "name": "Recognized"}],
            },
        },
        {"name": "last_edited_date_et", "type": "esriFieldTypeDate"},
    ],
    "relationships": [
        # relationshipId (id) deliberately differs from relatedTableId --
        # confusing the two is the bug these tests exist to catch.
        {"id": 7, "name": "species", "relatedTableId": 10},
        {"id": 3, "name": "threat", "relatedTableId": 11},
        {"id": 0, "name": "criteria_site", "relatedTableId": 3},
    ],
}

# Table 10: species. Has its own date field, which must decode against
# THIS schema, not layer 0's.
TABLE10_META = {
    "name": "species",
    "maxRecordCount": 2,
    "fields": [
        {"name": "objectid", "type": "esriFieldTypeOID"},
        {"name": "site_id", "type": "esriFieldTypeDouble"},
        {"name": "common_name", "type": "esriFieldTypeString"},
        {"name": "species_code", "type": "esriFieldTypeString"},
        # The binomial arrives split apart: epithet here, genus in iba_genus.
        {"name": "sci_name", "type": "esriFieldTypeString"},
        {"name": "iba_genus", "type": "esriFieldTypeString"},
        {"name": "assessment_dt", "type": "esriFieldTypeDate"},
        {"name": "globalid", "type": "esriFieldTypeGlobalID"},
    ],
}

# The real site-202 record: Audubon's frozen taxonomy still calls the western
# rail "Clapper Rail" / clarai, which is eBird's code for a DIFFERENT species.
CLAPPER_RAIL = {
    "objectid": 7801,
    "site_id": 202.0,
    "species_code": "clarai",
    "sci_name": "longirostris",
    "iba_genus": "Rallus",
    "common_name": "Clapper Rail",
}

# Any other related table (threat, observation, ...) -- these tests only
# assert routing and paging for them, so a bare schema is enough.
GENERIC_TABLE_META = {
    "name": "related",
    "maxRecordCount": 2,
    "fields": [{"name": "objectid", "type": "esriFieldTypeOID"}],
}

# Matches a layer/table endpoint (".../FeatureServer/<id>") but not the
# /query or /queryRelatedRecords operations hanging off it.
_TABLE_URL_RE = re.compile(r"/FeatureServer/\d+$")

ANCHORAGE_COASTAL = {
    "objectid": 897,
    "site_id": 1004.0,
    "site_name": "Anchorage Coastal",
    "state": "Alaska",
    "state_abbr": "AK",
    "flyway": "Pacific",
    "acres": 33382.735577,
    "latitude": 61.125,
    "longitude": -149.977005,
    "ebird_link": "http://ebird.org/ebird/GuideMe?ibas=US-AK_1004",
    "iba_priority": "Continental",
    "iba_status": "Recognized",
    "created_user_et": "someone",
}


class FakeService:
    """Routes mocked GETs by URL, recording every request for assertions."""

    def __init__(self):
        self.calls = []  # list of (url, params)
        # Queued /query feature pages, popped in order. Each entry is a list
        # of attribute dicts.
        self.query_pages = []
        self.count = 1
        self.related_pages = []
        self.error = None  # ArcGIS-style error body to return from /query
        # Rings returned when a caller asks for geometry (get_iba_boundary).
        self.rings = None

    def response(self, payload, status=200):
        request = httpx.Request("GET", "https://example.com")
        return httpx.Response(status, json=payload, request=request)

    async def get(self, url, params=None):
        params = params or {}
        self.calls.append((url, params))

        if url.endswith("/FeatureServer/0"):
            return self.response(LAYER0_META)
        if url.endswith("/FeatureServer/10"):
            return self.response(TABLE10_META)
        if _TABLE_URL_RE.search(url):
            # Any other related table: a bare schema is enough, since these
            # tests only assert routing and paging for them.
            return self.response(GENERIC_TABLE_META)
        if url.endswith("/queryRelatedRecords"):
            page = self.related_pages.pop(0) if self.related_pages else []
            return self.response(
                {
                    "relatedRecordGroups": [
                        {
                            "objectId": 897,
                            "relatedRecords": [{"attributes": a} for a in page],
                        }
                    ]
                }
            )
        if url.endswith("/query"):
            if self.error:
                return self.response({"error": self.error})
            if params.get("returnCountOnly") == "true":
                return self.response({"count": self.count})
            if params.get("returnGeometry") == "true":
                if self.rings is None:
                    return self.response({"features": []})
                return self.response(
                    {
                        "features": [
                            {
                                "attributes": {"site_name": "Mission Bay"},
                                "geometry": {"rings": self.rings},
                            }
                        ]
                    }
                )
            page = self.query_pages.pop(0) if self.query_pages else []
            return self.response({"features": [{"attributes": a} for a in page]})
        raise AssertionError(f"Unexpected URL: {url}")

    def query_params(self):
        """Params of every /query call (excluding count-only)."""
        return [
            p
            for url, p in self.calls
            if url.endswith("/query") and p.get("returnCountOnly") != "true"
        ]

    def related_params(self):
        return [p for url, p in self.calls if url.endswith("/queryRelatedRecords")]


@pytest.fixture
def service():
    return FakeService()


@pytest.fixture
async def plugin(service):
    p = AudubonIBAPlugin({"enabled": True, "service_url": SERVICE_URL})
    assert await p.initialize() is True
    p.client = Mock(get=AsyncMock(side_effect=service.get))
    return p


async def call(plugin, tool, args):
    result = await plugin.execute_tool(tool, args)
    return result


# ── Config schema ───────────────────────────────────────────────────────


class TestConfigSchema:
    def test_minimal_config(self):
        cfg = AudubonIBAPluginConfig(service_url=SERVICE_URL)
        assert cfg.layer_id == 0
        assert cfg.timeout == 120
        assert cfg.token is None

    def test_trailing_slash_stripped(self):
        cfg = AudubonIBAPluginConfig(service_url=SERVICE_URL + "/")
        assert cfg.service_url == SERVICE_URL

    @pytest.mark.parametrize("bad", ["", "not-a-url", "ftp://x.com/svc"])
    def test_bad_url_rejected(self, bad):
        with pytest.raises(ValidationError):
            AudubonIBAPluginConfig(service_url=bad)

    def test_unknown_key_rejected(self):
        with pytest.raises(ValidationError):
            AudubonIBAPluginConfig(service_url=SERVICE_URL, catalog_path="x.json")


# ── Tool surface ────────────────────────────────────────────────────────


class TestToolSurface:
    def test_plugin_identity(self):
        p = AudubonIBAPlugin({"service_url": SERVICE_URL})
        assert p.plugin_name == "audubon_iba"
        assert p.plugin_type == PluginType.OPEN_DATA

    async def test_exposes_exactly_the_iba_tools(self, plugin):
        names = {t.name for t in plugin.get_tools()}
        assert names == {
            "find_ibas_near_point",
            "search_ibas",
            "get_iba_details",
            "get_iba_related_records",
            "get_iba_boundary",
            "list_distinct_values",
        }

    async def test_no_catalog_discovery_tools(self, plugin):
        """The fixed service has no catalog, so discovery tools must be gone."""
        names = {t.name for t in plugin.get_tools()}
        assert not names & {
            "search_datasets",
            "get_dataset",
            "get_aggregations",
            "get_layer_schema",
        }

    async def test_related_records_category_enum(self, plugin):
        tool = next(
            t for t in plugin.get_tools() if t.name == "get_iba_related_records"
        )
        enum = set(tool.input_schema["properties"]["category"]["enum"])
        assert {"species", "criteria", "habitat", "threat", "description"} <= enum

    async def test_duplicate_categories_are_not_advertised(self, plugin):
        """criteria_site == criteria and site_description == description
        returned byte-identical output; offering both made an agent guess."""
        tool = next(
            t for t in plugin.get_tools() if t.name == "get_iba_related_records"
        )
        enum = tool.input_schema["properties"]["category"]["enum"]
        assert "criteria_site" not in enum
        assert "site_description" not in enum
        assert len(enum) == 9

    async def test_every_advertised_category_is_described(self, plugin):
        """The aliases shipped with no description -- they echoed their own
        name. Whatever is offered must be explained."""
        tool = next(
            t for t in plugin.get_tools() if t.name == "get_iba_related_records"
        )
        description = tool.description
        for category in tool.input_schema["properties"]["category"]["enum"]:
            assert f"'{category}' =" in description

    async def test_alias_categories_still_resolve(self, plugin, service):
        """Dropped from the enum, but not broken for existing callers."""
        service.query_pages = [[{"objectid": 897, "site_name": "X"}]]
        service.related_pages = [[{"objectid": 1}]]
        result = await call(
            plugin,
            "get_iba_related_records",
            {"site_id": 1004, "category": "criteria_site"},
        )
        assert result.success is True
        # ...and it reports itself as the canonical category, with its purpose.
        assert "criteria records for" in result.content[0]["text"]

    async def test_unknown_tool_is_an_error(self, plugin):
        result = await call(plugin, "nope", {})
        assert result.success is False
        assert "Unknown tool" in result.error_message


# ── find_ibas_near_point ────────────────────────────────────────────────


class TestFindNearPoint:
    async def test_sends_wgs84_buffer_query(self, plugin, service):
        service.query_pages = [[ANCHORAGE_COASTAL]]
        result = await call(
            plugin,
            "find_ibas_near_point",
            {"lat": 61.17, "lng": -149.9, "radius_km": 40},
        )
        assert result.success is True

        params = service.query_params()[0]
        # lng,lat order -- ArcGIS takes x,y, and swapping them is the classic
        # silent-no-match bug.
        assert params["geometry"] == "-149.9,61.17"
        assert params["geometryType"] == "esriGeometryPoint"
        assert params["distance"] == 40
        assert params["units"] == "esriSRUnit_Kilometer"
        assert params["spatialRel"] == "esriSpatialRelIntersects"
        # WGS84 hard contract: without inSR the point is read as EPSG:3857
        # metres and matches nothing.
        assert params["inSR"] == 4326
        assert params["outSR"] == 4326
        assert params["returnGeometry"] == "false"

    async def test_formats_results_with_ebird_and_caveat(self, plugin, service):
        service.query_pages = [[ANCHORAGE_COASTAL]]
        result = await call(
            plugin, "find_ibas_near_point", {"lat": 61.17, "lng": -149.9}
        )
        text = result.content[0]["text"]
        assert "Anchorage Coastal" in text
        assert "site_id: 1004" in text
        assert "US-AK_1004" in text  # ebird_link surfaced
        assert "not a legal or regulatory status" in text

    async def test_default_radius_is_applied(self, plugin, service):
        service.query_pages = [[]]
        await call(plugin, "find_ibas_near_point", {"lat": 61.17, "lng": -149.9})
        assert service.query_params()[0]["distance"] == 25

    async def test_no_results_suggests_next_step(self, plugin, service):
        service.query_pages = [[]]
        result = await call(
            plugin, "find_ibas_near_point", {"lat": 0, "lng": 0, "radius_km": 1}
        )
        assert result.success is True
        assert "No Important Bird Areas" in result.content[0]["text"]

    @pytest.mark.parametrize(
        "args",
        [
            {"lat": 91, "lng": 0},
            {"lat": -91, "lng": 0},
            {"lat": 0, "lng": 181},
            {"lat": 0, "lng": -181},
            {"lat": 0, "lng": 0, "radius_km": 0},
            {"lat": 0, "lng": 0, "radius_km": -5},
            {"lat": 0, "lng": 0, "radius_km": 9999},
            {"lat": "sixty", "lng": 0},
        ],
    )
    async def test_out_of_range_input_rejected(self, plugin, args):
        result = await call(plugin, "find_ibas_near_point", args)
        assert result.success is False

    async def test_missing_coords_rejected(self, plugin):
        result = await call(plugin, "find_ibas_near_point", {"lat": 61.17})
        assert result.success is False
        assert "lng" in result.error_message


# ── search_ibas ─────────────────────────────────────────────────────────


class TestSearchIBAs:
    async def test_state_matches_name_or_abbreviation(self, plugin, service):
        service.query_pages = [[ANCHORAGE_COASTAL]]
        await call(plugin, "search_ibas", {"state": "AK"})
        where = service.query_params()[0]["where"]
        assert "UPPER(state) = UPPER('AK')" in where
        assert "UPPER(state_abbr) = UPPER('AK')" in where

    async def test_filters_combine_with_and(self, plugin, service):
        service.query_pages = [[ANCHORAGE_COASTAL]]
        await call(
            plugin,
            "search_ibas",
            {"state": "AK", "priority": "Global", "flyway": "Pacific"},
        )
        where = service.query_params()[0]["where"]
        assert where.count(" AND ") == 2
        assert "UPPER(iba_priority) = UPPER('Global')" in where
        assert "UPPER(flyway) = UPPER('Pacific')" in where

    async def test_name_is_case_insensitive_substring(self, plugin, service):
        service.query_pages = [[ANCHORAGE_COASTAL]]
        await call(plugin, "search_ibas", {"name": "coastal"})
        where = service.query_params()[0]["where"]
        assert "UPPER(site_name) LIKE UPPER('%coastal%')" in where

    async def test_no_filters_browses_everything(self, plugin, service):
        service.query_pages = [[ANCHORAGE_COASTAL]]
        await call(plugin, "search_ibas", {})
        assert service.query_params()[0]["where"] == "1=1"

    async def test_leads_with_total_matching(self, plugin, service):
        service.query_pages = [[ANCHORAGE_COASTAL]]
        service.count = 152
        result = await call(plugin, "search_ibas", {"state": "AK"})
        assert result.content[0]["text"].startswith("TOTAL MATCHING: 152")

    async def test_quote_in_name_is_escaped_not_injected(self, plugin, service):
        """A site name with an apostrophe must not break out of the literal."""
        service.query_pages = [[]]
        await call(plugin, "search_ibas", {"name": "O'Brien"})
        where = service.query_params()[0]["where"]
        assert "O''Brien" in where

    async def test_like_wildcards_are_escaped(self, plugin, service):
        """A user's literal % must not become a wildcard."""
        service.query_pages = [[]]
        await call(plugin, "search_ibas", {"name": "100%"})
        where = service.query_params()[0]["where"]
        assert "100\\%" in where
        assert "ESCAPE" in where

    async def test_injection_attempt_stays_inside_the_literal(self, plugin, service):
        """A hostile name is searched as text, never executed: the quote is
        doubled, so the value cannot break out of its string literal."""
        service.query_pages = [[]]
        result = await call(plugin, "search_ibas", {"name": "x'; DROP TABLE iba; --"})
        assert result.success is True
        where = service.query_params()[0]["where"]
        assert "x''; DROP TABLE iba; --" in where

    async def test_name_containing_sql_keyword_is_searchable(self, plugin, service):
        """Real site names contain SQL keywords ('Union Slough', 'Grant
        Lake'); quoted literal content must not trip the injection scan."""
        service.query_pages = [[]]
        result = await call(plugin, "search_ibas", {"name": "Union Slough"})
        assert result.success is True
        assert "'%Union Slough%'" in service.query_params()[0]["where"]


# ── get_iba_details ─────────────────────────────────────────────────────


class TestGetDetails:
    async def test_queries_by_site_id_without_float_noise(self, plugin, service):
        service.query_pages = [[ANCHORAGE_COASTAL]]
        await call(plugin, "get_iba_details", {"site_id": 1004})
        assert service.query_params()[0]["where"] == "site_id = 1004"

    async def test_surfaces_the_ebird_bridge_prominently(self, plugin, service):
        """The centroid is the only working route to eBird, so it leads --
        above the field dump, not buried in it."""
        service.query_pages = [[ANCHORAGE_COASTAL]]
        result = await call(plugin, "get_iba_details", {"site_id": 1004})
        text = result.content[0]["text"]
        bridge_pos = text.index("eBird bridge")
        record_pos = text.index("Full record:")
        assert bridge_pos < record_pos

    async def test_suppresses_editor_tracking_noise(self, plugin, service):
        service.query_pages = [[ANCHORAGE_COASTAL]]
        result = await call(plugin, "get_iba_details", {"site_id": 1004})
        assert "created_user_et" not in result.content[0]["text"]

    async def test_lists_related_categories_as_next_step(self, plugin, service):
        service.query_pages = [[ANCHORAGE_COASTAL]]
        result = await call(plugin, "get_iba_details", {"site_id": 1004})
        assert "get_iba_related_records" in result.content[0]["text"]

    async def test_missing_site_says_so(self, plugin, service):
        service.query_pages = [[]]
        result = await call(plugin, "get_iba_details", {"site_id": 99999999})
        assert result.success is True
        assert "No IBA found with site_id 99999999" in result.content[0]["text"]

    @pytest.mark.parametrize("bad", [None, "abc", float("nan"), float("inf"), True])
    async def test_non_numeric_site_id_rejected(self, plugin, bad):
        result = await call(plugin, "get_iba_details", {"site_id": bad})
        assert result.success is False


# ── get_iba_related_records ─────────────────────────────────────────────


class TestRelatedRecords:
    async def test_resolves_site_id_to_objectid_then_uses_relationship_id(
        self, plugin, service
    ):
        """queryRelatedRecords keys on objectid and relationshipId.

        relationshipId (7) is NOT the related table id (10); sending the
        table id would silently query the wrong relationship.
        """
        service.query_pages = [[{"objectid": 897, "site_name": "Anchorage Coastal"}]]
        service.related_pages = [[{"objectid": 1, "common_name": "Snow Goose"}]]

        result = await call(
            plugin,
            "get_iba_related_records",
            {"site_id": 1004, "category": "species"},
        )
        assert result.success is True

        # site_id -> objectid resolution happened first
        assert service.query_params()[0]["where"] == "site_id = 1004"

        params = service.related_params()[0]
        assert params["objectIds"] == 897
        assert params["relationshipId"] == 7
        assert "Snow Goose" in result.content[0]["text"]

    async def test_decodes_dates_against_the_related_tables_schema(
        self, plugin, service
    ):
        """A species date must decode via table 10's schema, not layer 0's."""
        service.query_pages = [[{"objectid": 897, "site_name": "Anchorage Coastal"}]]
        service.related_pages = [
            [{"common_name": "Snow Goose", "assessment_dt": 1331683200000}]
        ]
        result = await call(
            plugin,
            "get_iba_related_records",
            {"site_id": 1004, "category": "species"},
        )
        text = result.content[0]["text"]
        assert "2012-03-14" in text
        assert "1331683200000" not in text

    async def test_category_aliases_map_to_relationship_names(self, plugin, service):
        service.query_pages = [[{"objectid": 897, "site_name": "X"}]]
        service.related_pages = [[{"objectid": 1}]]
        result = await call(
            plugin, "get_iba_related_records", {"site_id": 1004, "category": "threat"}
        )
        assert result.success is True
        assert service.related_params()[0]["relationshipId"] == 3

    async def test_observation_warns_records_are_historical(self, plugin, service):
        """The observation table is stale; the model should prefer eBird."""
        LAYER0_META["relationships"].append(
            {"id": 6, "name": "observation", "relatedTableId": 7}
        )
        try:
            service.query_pages = [[{"objectid": 897, "site_name": "X"}]]
            service.related_pages = [[{"objectid": 1}]]
            result = await call(
                plugin,
                "get_iba_related_records",
                {"site_id": 1004, "category": "observation"},
            )
            text = result.content[0]["text"]
            assert "historical" in text
            # Point at the centroid, not at ebird_link -- which eBird rejects.
            assert "centroid" in text
        finally:
            LAYER0_META["relationships"].pop()

    async def test_unknown_category_rejected(self, plugin):
        result = await call(
            plugin, "get_iba_related_records", {"site_id": 1004, "category": "bogus"}
        )
        assert result.success is False
        assert "Unknown category" in result.error_message

    async def test_missing_site_rejected(self, plugin, service):
        service.query_pages = [[]]
        result = await call(
            plugin,
            "get_iba_related_records",
            {"site_id": 12345, "category": "species"},
        )
        assert result.success is False
        assert "No IBA found" in result.error_message

    async def test_empty_related_set_explains_itself(self, plugin, service):
        service.query_pages = [[{"objectid": 897, "site_name": "X"}]]
        service.related_pages = [[]]
        result = await call(
            plugin,
            "get_iba_related_records",
            {"site_id": 1004, "category": "species"},
        )
        assert result.success is True
        # Absence of a record is not evidence the attribute doesn't apply.
        assert "not entered" in result.content[0]["text"]

    async def test_pages_past_the_record_cap(self, plugin, service):
        """Table 10's maxRecordCount is 2, so 3 records need two round-trips."""
        service.query_pages = [[{"objectid": 897, "site_name": "X"}]]
        service.related_pages = [
            [{"common_name": "A"}, {"common_name": "B"}],
            [{"common_name": "C"}],
        ]
        result = await call(
            plugin,
            "get_iba_related_records",
            {"site_id": 1004, "category": "species", "limit": 10},
        )
        calls = service.related_params()
        assert len(calls) == 2
        assert calls[0]["resultRecordCount"] == 2
        assert calls[1]["resultOffset"] == 2
        text = result.content[0]["text"]
        assert "Returned 3 record(s)" in text


# ── Cross-server composability with eBird ───────────────────────────────


class TestEBirdBridge:
    """The seam with the eBird MCP server, where wrong answers were silent.

    Audubon's species codes are NOT eBird's, and its ebird_link is not an API
    identifier -- but both look like they are, so a naive chain produced
    confident, correct-looking zeroes.
    """

    async def test_species_records_warn_codes_are_not_ebird_codes(
        self, plugin, service
    ):
        service.query_pages = [[{"objectid": 897, "site_name": "Mission Bay"}]]
        service.related_pages = [[CLAPPER_RAIL]]
        result = await call(
            plugin, "get_iba_related_records", {"site_id": 202, "category": "species"}
        )
        text = result.content[0]["text"]
        assert "do NOT pass `species_code` to eBird" in text
        assert "SILENTLY" in text

    async def test_known_split_is_called_out_by_name(self, plugin, service):
        """The flagship failure: 'clarai' is a valid eBird code for an
        Atlantic species, so eBird answers 'no records' for a rail that is
        present. Name the successor rather than let the zero stand."""
        service.query_pages = [[{"objectid": 897, "site_name": "Mission Bay"}]]
        service.related_pages = [[CLAPPER_RAIL]]
        result = await call(
            plugin, "get_iba_related_records", {"site_id": 202, "category": "species"}
        )
        text = result.content[0]["text"]
        assert "ridrai1" in text
        assert "Ridgway's Rail" in text

    async def test_species_record_composes_the_binomial(self, plugin, service):
        """sci_name is a bare epithet; the genus lives in iba_genus. Read
        alone, sci_name is half a name and a crosswalk keyed on it mismatches."""
        service.query_pages = [[{"objectid": 897, "site_name": "Mission Bay"}]]
        service.related_pages = [[CLAPPER_RAIL]]
        result = await call(
            plugin, "get_iba_related_records", {"site_id": 202, "category": "species"}
        )
        text = result.content[0]["text"]
        assert "scientific_name: Rallus longirostris" in text
        # raw fields kept for provenance
        assert "sci_name: longirostris" in text
        assert "iba_genus: Rallus" in text

    async def test_details_emit_a_site_sized_radius(self, plugin, service):
        """eBird's nearby default is 25 km; Mission Bay is ~12 km2, so an
        equal-area circle is ~2 km. Give the agent the number."""
        service.query_pages = [
            [dict(ANCHORAGE_COASTAL, site_id=202, acres=2970.01334296)]
        ]
        result = await call(plugin, "get_iba_details", {"site_id": 202})
        text = result.content[0]["text"]
        assert "suggested_radius_km: 2" in text

    async def test_details_route_to_ebird_via_the_centroid(self, plugin, service):
        service.query_pages = [[ANCHORAGE_COASTAL]]
        result = await call(plugin, "get_iba_details", {"site_id": 1004})
        text = result.content[0]["text"]
        assert "get_nearby_hotspots" in text
        assert "61.125" in text and "-149.977005" in text

    async def test_ebird_link_is_not_sold_as_an_api_identifier(self, plugin, service):
        """The link is a human 'GuideMe' URL; the US-CA_202 inside it is an
        Audubon id that every eBird tool rejects."""
        service.query_pages = [[ANCHORAGE_COASTAL]]
        result = await call(plugin, "get_iba_details", {"site_id": 1004})
        text = result.content[0]["text"]
        assert "for humans" in text
        assert "NOT an API identifier" in text
        # the old claim is gone
        assert "pre-joins this IBA to its eBird hotspot/region" not in text


# ── get_iba_boundary ────────────────────────────────────────────────────


class TestBoundary:
    SQUARE = [[[-117.25, 32.75], [-117.20, 32.75], [-117.20, 32.80], [-117.25, 32.80]]]

    async def test_returns_wgs84_rings_and_bbox(self, plugin, service):
        service.rings = self.SQUARE
        result = await call(plugin, "get_iba_boundary", {"site_id": 202})
        assert result.success is True
        text = result.content[0]["text"]
        assert "west -117.250000" in text
        assert "north 32.800000" in text
        assert "1 ring(s), 4 vertices" in text

        params = service.query_params()[0]
        assert params["returnGeometry"] == "true"
        assert params["outSR"] == 4326
        assert params["maxAllowableOffset"] == 0.0001

    async def test_full_precision_when_simplify_false(self, plugin, service):
        service.rings = self.SQUARE
        await call(plugin, "get_iba_boundary", {"site_id": 202, "simplify": False})
        assert "maxAllowableOffset" not in service.query_params()[0]

    async def test_explains_that_a_radius_is_not_the_boundary(self, plugin, service):
        service.rings = self.SQUARE
        result = await call(plugin, "get_iba_boundary", {"site_id": 202})
        assert (
            "A centroid radius alone is NOT the boundary" in (result.content[0]["text"])
        )

    async def test_missing_site_says_so(self, plugin, service):
        service.rings = None
        result = await call(plugin, "get_iba_boundary", {"site_id": 99999})
        assert result.success is True
        assert "No IBA found" in result.content[0]["text"]

    async def test_bad_site_id_rejected(self, plugin):
        result = await call(plugin, "get_iba_boundary", {"site_id": "abc"})
        assert result.success is False


# ── list_distinct_values ────────────────────────────────────────────────


class TestDistinctValues:
    async def test_returns_distinct_values(self, plugin, service):
        service.query_pages = [[{"iba_priority": "Continental"}, {"iba_priority": "G"}]]
        result = await call(plugin, "list_distinct_values", {"field": "iba_priority"})
        params = service.query_params()[0]
        assert params["returnDistinctValues"] == "true"
        assert params["outFields"] == "iba_priority"

        text = result.content[0]["text"]
        # 'G' is the stored code; 'Global' is its coded-domain label.
        assert "Global" in text
        assert "Continental" in text

    async def test_unknown_field_rejected_with_guidance(self, plugin):
        result = await call(plugin, "list_distinct_values", {"field": "priorty"})
        assert result.success is False
        assert "CASE-SENSITIVE" in result.error_message

    async def test_field_name_is_validated_against_schema(self, plugin):
        """The field lands in outFields, so it is a security boundary."""
        result = await call(
            plugin, "list_distinct_values", {"field": "site_id,password"}
        )
        assert result.success is False

    async def test_case_mismatch_is_rejected(self, plugin):
        result = await call(plugin, "list_distinct_values", {"field": "IBA_PRIORITY"})
        assert result.success is False


# ── Pagination & error handling ─────────────────────────────────────────


class TestPaginationAndErrors:
    async def test_search_pages_past_record_cap(self, plugin, service):
        """Layer 0's maxRecordCount is 2, so a limit of 3 needs two pages."""
        service.query_pages = [
            [dict(ANCHORAGE_COASTAL, site_id=1), dict(ANCHORAGE_COASTAL, site_id=2)],
            [dict(ANCHORAGE_COASTAL, site_id=3)],
        ]
        result = await call(plugin, "search_ibas", {"limit": 3})
        params = service.query_params()
        assert len(params) == 2
        assert params[0]["resultRecordCount"] == 2
        assert params[1]["resultOffset"] == 2
        assert "Returned 3 IBA(s)" in result.content[0]["text"]

    async def test_short_page_stops_pagination(self, plugin, service):
        service.query_pages = [[ANCHORAGE_COASTAL]]  # 1 < cap of 2 -> done
        await call(plugin, "search_ibas", {"limit": 100})
        assert len(service.query_params()) == 1

    async def test_arcgis_error_body_is_surfaced(self, plugin, service):
        """ArcGIS reports failures with HTTP 200 and an error object."""
        service.error = {"code": 400, "message": "Unable to perform query"}
        result = await call(plugin, "search_ibas", {"state": "AK"})
        assert result.success is False
        assert "code 400" in result.error_message
        assert "Unable to perform query" in result.error_message

    async def test_token_error_gets_a_hint(self, plugin, service):
        service.error = {"code": 499, "message": "Token Required"}
        result = await call(plugin, "search_ibas", {})
        assert result.success is False
        assert "service_url" in result.error_message

    async def test_http_error_is_surfaced(self, plugin):
        request = httpx.Request("GET", "https://example.com")
        response = httpx.Response(503, text="upstream down", request=request)
        plugin.client = Mock(
            get=AsyncMock(
                side_effect=httpx.HTTPStatusError(
                    "boom", request=request, response=response
                )
            )
        )
        result = await call(plugin, "search_ibas", {})
        assert result.success is False
        assert "503" in result.error_message

    async def test_network_error_is_surfaced(self, plugin):
        plugin.client = Mock(
            get=AsyncMock(side_effect=httpx.ConnectError("no route to host"))
        )
        result = await call(plugin, "search_ibas", {})
        assert result.success is False
        assert "Could not reach the IBA service" in result.error_message

    async def test_metadata_is_fetched_once_and_cached(self, plugin, service):
        service.query_pages = [[ANCHORAGE_COASTAL], [ANCHORAGE_COASTAL]]
        await call(plugin, "search_ibas", {"limit": 1})
        await call(plugin, "search_ibas", {"limit": 1})
        meta_calls = [u for u, _ in service.calls if u.endswith("/FeatureServer/0")]
        assert len(meta_calls) == 1


# ── Lifecycle ───────────────────────────────────────────────────────────


class TestLifecycle:
    async def test_initialize_and_shutdown(self):
        p = AudubonIBAPlugin({"enabled": True, "service_url": SERVICE_URL})
        assert await p.initialize() is True
        assert p.is_initialized is True
        await p.shutdown()
        assert p.is_initialized is False
        assert p.client is None

    async def test_initialize_fails_on_bad_config(self):
        p = AudubonIBAPlugin({"enabled": True, "service_url": "nonsense"})
        assert await p.initialize() is False

    async def test_health_check_ok(self, plugin):
        assert await plugin.health_check() is True

    async def test_health_check_fails_when_service_is_down(self, plugin):
        plugin.client = Mock(get=AsyncMock(side_effect=httpx.ConnectError("down")))
        assert await plugin.health_check() is False


# ── Helpers ─────────────────────────────────────────────────────────────


class TestHelpers:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (1004.0, "1004"),  # site_id is a Double but always integral
            (1004, "1004"),
            (33382.735577, "33382.735577"),
            (1e20, "100000000000000000000"),  # never scientific notation
            (None, ""),
        ],
    )
    def test_fmt_number(self, value, expected):
        assert _fmt_number(value) == expected

    def test_compose_binomial(self):
        assert (
            _compose_binomial({"iba_genus": "Rallus", "sci_name": "longirostris"})
            == "Rallus longirostris"
        )
        # Half a name is no name -- don't emit a misleading partial.
        assert _compose_binomial({"sci_name": "longirostris"}) == ""
        assert _compose_binomial({"iba_genus": "Rallus"}) == ""

    @pytest.mark.parametrize(
        "acres,expected",
        [
            (2970.01334296, 2.0),  # Mission Bay: ~12 km2 -> ~1.96 km
            (None, None),
            (0, None),
            (-5, None),
            (1, 1.0),  # tiny site still gets a usable radius
            (10_000_000, 50.0),  # capped at eBird's max nearby radius
        ],
    )
    def test_suggested_radius_km(self, acres, expected):
        assert _suggested_radius_km(acres) == expected

    def test_escape_like(self):
        assert _escape_like("100%") == "100\\%"
        assert _escape_like("a_b") == "a\\_b"

    def test_validate_site_id_accepts_numeric_strings(self):
        assert _validate_site_id("1004") == 1004.0

    @pytest.mark.parametrize("bad", [None, "abc", True, float("nan"), float("inf")])
    def test_validate_site_id_rejects_junk(self, bad):
        with pytest.raises(ValueError):
            _validate_site_id(bad)
