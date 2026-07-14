"""Production smoke test for the Audubon Important Bird Areas MCP server.

Exercises the JSON-RPC surface and the full audubon_iba tool chain
end-to-end against the deployed Lambda, finishing with the "verification
query": a point-buffer search around Anchorage (61.17, -149.9, 40 km) that
must return Alaska IBAs, followed by a related-record traversal that pulls
one of those sites' species list. Together those two prove the WGS84 buffer
contract and the site_id -> objectid -> queryRelatedRecords chain.

Read-only; paces calls to stay under the API Gateway rate limit (5 rps) and
WAF per-IP cap (300/5min).

Usage:
    python3 scripts/smoke_prod.py [URL]

URL defaults to the production custom domain; override with an argument or
the OPENCONTEXT_SMOKE_URL env var to point at a different deployment
(e.g. the raw API Gateway URL or a local server).
"""

import json
import os
import sys
import time
import urllib.request

URL = (
    (sys.argv[1] if len(sys.argv) > 1 else None)
    or os.environ.get("OPENCONTEXT_SMOKE_URL")
    or "https://audubon-iba.codeforanchorage.org/mcp"
)

# Anchorage. Chosen because it sits inside a dense cluster of Alaska IBAs,
# so a 40 km buffer must return several -- an empty result here means the
# inSR=4326 contract broke and the point was read as Web Mercator metres.
ANCHORAGE_LAT = 61.17
ANCHORAGE_LNG = -149.9
RADIUS_KM = 40

_id = 0
results = []


def rpc(method, params=None):
    global _id
    _id += 1
    payload = {"jsonrpc": "2.0", "id": _id, "method": method}
    if params is not None:
        payload["params"] = params
    req = urllib.request.Request(
        URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        body = json.loads(r.read().decode())
    time.sleep(0.4)  # pace under 5 rps
    return body


def call_tool(name, args):
    return rpc("tools/call", {"name": f"audubon_iba__{name}", "arguments": args})


def text_of(resp):
    return resp["result"]["content"][0]["text"]


def check(label, ok, detail=""):
    results.append(ok)
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}" + (f" -- {detail}" if detail else ""))


print(f"Smoke testing: {URL}\n")

# 1. ping
try:
    r = rpc("ping")
    check("ping", r.get("result", {}).get("status") == "ok", str(r.get("result")))
except Exception as e:
    check("ping", False, repr(e))

# 2. initialize -- must carry the server-level instructions, including the
#    "no legal force" guardrail that keeps IBAs from being read as zoning.
try:
    r = rpc(
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "smoke", "version": "1.0"},
        },
    )
    result = r["result"]
    instructions = result.get("instructions", "")
    ok = bool(result["serverInfo"]["name"]) and "NO legal or regulatory" in instructions
    check("initialize (instructions present)", ok, f"{len(instructions)} chars")
except Exception as e:
    check("initialize (instructions present)", False, repr(e))

# 3. tools/list -- the six IBA tools, and none of the catalog-discovery
#    tools the fixed-service design deliberately drops.
try:
    r = rpc("tools/list")
    tools = {t["name"] for t in r["result"]["tools"]}
    expected = {
        "audubon_iba__find_ibas_near_point",
        "audubon_iba__search_ibas",
        "audubon_iba__get_iba_details",
        "audubon_iba__get_iba_related_records",
        "audubon_iba__get_iba_boundary",
        "audubon_iba__list_distinct_values",
    }
    check("tools/list (6 tools)", tools == expected, f"{sorted(tools)}")
except Exception as e:
    check("tools/list (6 tools)", False, repr(e))

# 4. VERIFICATION QUERY (1/2) -- point-buffer search around Anchorage.
site_id = None
try:
    t = text_of(
        call_tool(
            "find_ibas_near_point",
            {"lat": ANCHORAGE_LAT, "lng": ANCHORAGE_LNG, "radius_km": RADIUS_KM},
        )
    )
    ok = "Anchorage Coastal" in t and "site_id:" in t
    if ok:
        # Grab a site_id to traverse in the next step.
        for line in t.splitlines():
            if line.strip().startswith("site_id:"):
                site_id = int(line.split(":")[1].strip())
                break
    check(
        f"verification query 1: find_ibas_near_point({ANCHORAGE_LAT}, "
        f"{ANCHORAGE_LNG}, {RADIUS_KM}km)",
        ok and site_id is not None,
        f"first site_id={site_id}" if ok else "ERROR/empty: " + t[:80],
    )
except Exception as e:
    check("verification query 1: find_ibas_near_point(Anchorage)", False, repr(e))

# 5. VERIFICATION QUERY (2/2) -- related-record traversal for that site.
#    Proves site_id -> objectid resolution and queryRelatedRecords wiring.
try:
    if site_id is None:
        raise RuntimeError("no site_id from the buffer query")
    t = text_of(
        call_tool(
            "get_iba_related_records",
            {"site_id": site_id, "category": "species", "limit": 5},
        )
    )
    ok = "Record 1:" in t and "common_name:" in t
    check(
        f"verification query 2: get_iba_related_records({site_id}, 'species')",
        ok,
        t.split("\n")[0][:70] if ok else "ERROR/empty: " + t[:80],
    )
except Exception as e:
    check("verification query 2: get_iba_related_records(species)", False, repr(e))

# 5b. The species table must warn that its codes are NOT eBird's. Audubon's
#     taxonomy is frozen pre-2014 and most codes coincide with eBird's, so a
#     stale one fails silently -- eBird returns a believable "no records" for
#     a bird that is present. If this warning ever goes missing, the mashup
#     starts producing confident wrong answers again.
try:
    if site_id is None:
        raise RuntimeError("no site_id from the buffer query")
    t = text_of(
        call_tool(
            "get_iba_related_records",
            {"site_id": site_id, "category": "species", "limit": 5},
        )
    )
    ok = "do NOT pass `species_code` to eBird" in t and "scientific_name:" in t
    check(
        "species records carry the taxonomy warning + composed binomial",
        ok,
        "warning + scientific_name present" if ok else "MISSING: " + t[:80],
    )
except Exception as e:
    check("species records carry the taxonomy warning", False, repr(e))

# 6. get_iba_details -- the centroid is the ONLY working bridge to eBird
#    (eBird has no notion of an IBA id). ebird_link must NOT be sold as an
#    API identifier: it is a human web URL, and feeding the US-CA_202 inside
#    it to eBird gets a rejection.
try:
    t = text_of(call_tool("get_iba_details", {"site_id": site_id or 1004}))
    ok = (
        "eBird bridge" in t
        and "suggested_radius_km:" in t
        and "NOT an API identifier" in t
        and "ebird.org" in t
    )
    check(
        "get_iba_details (centroid bridge, not a fake join key)",
        ok,
        t.split("\n")[0][:60],
    )
except Exception as e:
    check("get_iba_details (centroid bridge)", False, repr(e))

# 6b. get_iba_boundary -- real polygon rings. Without these, "inside the IBA"
#     can only be approximated by a radius around the centroid, which for an
#     estuary pulls in inland hotspots and misses the river channel.
try:
    t = text_of(call_tool("get_iba_boundary", {"site_id": site_id or 1004}))
    ok = "Bounding box (WGS84):" in t and "Ring 1" in t
    check(
        "get_iba_boundary (WGS84 rings + bbox)",
        ok,
        t.split("\n")[3][:60] if ok else "ERROR/empty: " + t[:80],
    )
except Exception as e:
    check("get_iba_boundary (WGS84 rings + bbox)", False, repr(e))

# 6c. Regression: a site name containing a SQL keyword must be searchable.
#     'Union' was rejected as injection, which hid real IBAs entirely.
try:
    t = text_of(call_tool("search_ibas", {"name": "Union", "limit": 3}))
    ok = "TOTAL MATCHING:" in t and "Union" in t and "Forbidden" not in t
    check(
        "search_ibas(name='Union') -- SQL keyword in a site name",
        ok,
        t.split("\n")[0][:60] if ok else "REJECTED: " + t[:80],
    )
except Exception as e:
    check("search_ibas(name='Union')", False, repr(e))

# 7. search_ibas -- attribute filters + TOTAL MATCHING count
try:
    t = text_of(
        call_tool("search_ibas", {"state": "AK", "priority": "Global", "limit": 3})
    )
    ok = "TOTAL MATCHING:" in t and "site_id:" in t
    check("search_ibas(state=AK, priority=Global)", ok, t.split("\n")[0][:60])
except Exception as e:
    check("search_ibas(state=AK, priority=Global)", False, repr(e))

# 8. search_ibas -- case-insensitive name LIKE
try:
    t = text_of(call_tool("search_ibas", {"name": "coastal", "limit": 3}))
    ok = "TOTAL MATCHING:" in t and "Coastal" in t
    check("search_ibas(name='coastal') case-insensitive", ok, t.split("\n")[0][:60])
except Exception as e:
    check("search_ibas(name='coastal') case-insensitive", False, repr(e))

# 9. list_distinct_values -- the priority enum
try:
    t = text_of(call_tool("list_distinct_values", {"field": "iba_priority"}))
    ok = "Global" in t and "Continental" in t and "State" in t
    check("list_distinct_values('iba_priority')", ok, t.split("\n")[0][:60])
except Exception as e:
    check("list_distinct_values('iba_priority')", False, repr(e))

# 10. error path -- an unknown field must be rejected, not silently queried
try:
    r = call_tool("list_distinct_values", {"field": "priorty"})
    ok = r["result"].get("isError") is True
    check("unknown field rejected", ok, str(r["result"].get("error"))[:60])
except Exception as e:
    check("unknown field rejected", False, repr(e))

print()
passed = sum(1 for r in results if r)
total = len(results)
print(f"{passed}/{total} checks passed")
sys.exit(0 if passed == total else 1)
