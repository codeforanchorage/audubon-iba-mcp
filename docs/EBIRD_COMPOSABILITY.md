# Composing Audubon IBA with eBird

The Audubon IBA server tells you **where the conservation priorities are**. eBird tells you **what birds are actually being seen**. Chaining them answers the question the whole mashup exists for:

> Which of this IBA's designated species were reported inside its boundary recently?

The two datasets share **no keys**. Worse, there are three places where they *look* like they share a key — and following that appearance produces a confident, plausible, **wrong** answer rather than an error. This document is the map of those traps and the way through them.

Every claim below was verified against the live Audubon FeatureServer and the live eBird API on **2026-07-13**, using **Mission Bay – San Diego River Estuary** (`site_id: 202`, centroid `32.7783, -117.2302`).

---

## Trap 1: `species_code` is not eBird's `speciesCode`

This is the dangerous one, because it fails **silently**.

Audubon's `species` table carries its own `species_code` values on a taxonomy frozen at roughly the pre-2014 AOU checklist. eBird uses a different namespace on current, post-split taxonomy.

The trap is not that the codes are *different*. It is that **most of them are identical**. For site 202, five of eight designated species (`brant`, `marwre`, `savspa`, `logshr`, `loncur`) are valid eBird codes for the correct bird. A pipeline that passes codes straight through therefore *appears to work* — until it hits one that doesn't.

### The worked example

Site 202 is designated for **Clapper Rail**, `species_code: clarai`:

```
audubon_iba__get_iba_related_records(site_id=202, category="species")
  → common_name: "Clapper Rail", species_code: "clarai"

ebird__get_nearby_observations_for_species(lat=32.7783, lng=-117.2302, speciesCode="clarai")
  → "No eBird records of 'clarai' near the query point"
  → "⚠️ ABSENCE-OF-EVIDENCE: this does NOT mean 'clarai' is absent…"
```

Read at face value: *the rail isn't being seen here.* That is false. The bird is present, on multiple recent checklists:

```
ebird__get_nearby_observations_for_species(…, speciesCode="ridrai1", dist=2)
  → Ridgway's Rail (Rallus obsoletus) — Kendall-Frost Marsh Reserve (L469984)
```

**Root cause.** In 2014 *Rallus longirostris* was split. `clarai` remains a **valid** eBird code — it now means Clapper Rail (*Rallus crepitans*), an Atlantic/Gulf bird that **does not occur in California**. The western population became Ridgway's Rail (*Rallus obsoletus*, `ridrai1`). Audubon never followed the split, so its record for a California site points at the wrong taxon.

The query was well-formed, the code was real, the answer was zero, and the zero was wrong. eBird's absence-of-evidence disclaimer — which is excellent, and correct in general — makes that zero read as a deliberate "not reported lately" rather than "you asked about the wrong bird."

### The two failure classes

Every stale code was probed against the live eBird API. They fall into two classes, and **only one is dangerous**:

| Class | What eBird does | Why it matters |
| ----- | --------------- | -------------- |
| **SILENT** | Code is still **valid**, but denotes a different (or effectively unreportable) taxon → returns a believable *"no records"* | **Dangerous.** Indistinguishable from a genuine absence. |
| **LOUD** | Code is **unknown** to eBird → `HTTP 400 unknown_species` | Harmless. You cannot miss it. |

Confirmed **SILENT** (verified: valid code, returns a false zero):

| Audubon code | Audubon calls it | What eBird actually gives you | Resolve to |
| ------------ | ---------------- | ----------------------------- | ---------- |
| `clarai` | Clapper Rail | *Rallus crepitans* — Atlantic/Gulf, absent from the Pacific coast | **`ridrai1`** (Ridgway's Rail) on western sites; `clarai` is *correct* on Gulf/Atlantic sites |
| `commoo` | Common Moorhen | The Old World bird — a false zero **anywhere in the Americas** | **`comgal1`** (Common Gallinule) |
| `yelwar4` | Yellow-rumped Warbler | Resolves, but returns nothing **anywhere** | **`yerwar`** (*Setophaga coronata*) |
| `sagspa` | Sage Sparrow | Resolves, yields no records | Bell's Sparrow (coastal/CA) **or** Sagebrush Sparrow (interior) — region-dependent |
| `winwre` | Winter Wren | Resolves, yields no records | Winter Wren (eastern) **or** Pacific Wren (western) — region-dependent |

Confirmed **LOUD** (eBird rejects outright; successor named to save a round-trip):

| Audubon code | Successor |
| ------------ | --------- |
| `wesscr1` | Western Scrub-Jay was split in 2016 → California Scrub-Jay (**`cowscj1`**) / Woodhouse's Scrub-Jay |
| `whippo` | Whip-poor-will split in 2010 → Eastern / Mexican Whip-poor-will |
| `blasco` | Audubon's *Melanitta nigra* is now the Old World bird; the American bird is Black Scoter (*M. americana*) |
| `santer` | Sandwich Tern's New World population is treated as Cabot's Tern by some authorities |

`commoo` and `yelwar4` deserve emphasis: moorhens/gallinules and Yellow-rumped Warblers are **very widely designated**, so these two silently zero out common species across many sites — not just one rail in one estuary.

> **This table is a hint, not an authority.** It is a curated safety net for the landmines, seeded by probing the live API. It is not a complete crosswalk, and the correct successor is often **region-dependent**. Always confirm against eBird's live taxonomy before reporting.

### What the server does about it

`get_iba_related_records(category="species")` now:

1. Emits a **`TAXONOMY WARNING`** on every species result: do not pass `species_code` to eBird.
2. Emits a composed **`scientific_name`** to resolve against (see Trap 3).
3. Flags any species in the table above with its successor.

### The correct procedure

Resolve **by name, never by code**:

```
1. get_iba_related_records(site_id, "species")     → common_name + scientific_name
2. ebird__get_taxonomy                             → look up the CURRENT speciesCode
3. ebird__get_nearby_observations_for_species(…)   → query with the resolved code
```

The scientific name may *itself* be stale (`Rallus longirostris` no longer exists as a species). It is a **lookup key**, not the answer. If a name doesn't resolve cleanly, that is the signal you've hit a split — not a reason to fall back to the raw code.

---

## Trap 2: `ebird_link` is not a join key

`get_iba_details` returns an `ebird_link`. It is tempting, and it is a dead end for machines:

```
http://ebird.org/ebird/GuideMe?…&ibas=US-CA_202

ebird__get_recent_observations(regionCode="US-CA_202")
  → ERROR: Invalid regionCode: 'US-CA_202'
```

`US-CA_202` is an **Audubon IBA identifier**, not an eBird `regionCode` or hotspot `locId`. The link is a legacy human-facing "GuideMe" wizard URL — fine to hand a person, useless to a tool. **eBird has no concept of an IBA id.**

### The bridge that works: the centroid

```
get_iba_details(202)
  → lat: 32.7783, lng: -117.2302
  → suggested_radius_km: 2
```

Then:

```
ebird__get_nearby_hotspots(lat, lng, dist=suggested_radius_km)
  → per hotspot: ebird__get_recent_observations(locId)
```

### Why `suggested_radius_km` exists

eBird's `nearby_*` tools default to **`dist=25` km**. Site 202 is 2,970 acres ≈ **12 km²** — an equal-area circle has a radius of **~1.96 km**. The default over-scans by roughly **160×**, dragging in a whole metro's worth of hotspots and quietly turning "in the IBA" into "somewhere in San Diego."

So `get_iba_details` computes it for you:

```
suggested_radius_km = sqrt(acres × 0.00404685642 / π)     # floored at 1 km, capped at eBird's 50 km max
```

---

## Trap 3: `sci_name` is only half a name

```
common_name: "Clapper Rail"
sci_name:    "longirostris"      ← just the epithet
iba_genus:   "Rallus"            ← the genus lives here
```

Reading `sci_name` as a scientific name gets you `longirostris`, and any crosswalk keyed on it will mismatch. The server composes the binomial for you and emits it **above** the raw fields (which are retained for provenance):

```
scientific_name: Rallus longirostris
```

Again: composed ≠ current. `Rallus longirostris` is exactly the stale name that Trap 1 is about.

---

## Trap 4: a radius is not a boundary

An IBA is an estuary, a river channel, a ridgeline — not a disc. A radius around the centroid **over-includes** neighbouring land and **under-includes** the far ends of the site. At `dist=4` km from site 202's centroid, eBird returns Tecolote Canyon, University of San Diego, and Western Hills Park — all inland, none in the estuary.

For a true in-boundary audit, use the real polygon:

```
get_iba_boundary(202)
  → Bounding box (WGS84): west -117.260359, south 32.748051, east -117.205339, north 32.797006
  → 4 ring(s), 290 vertices (simplified)
```

Then keep only the hotspots whose coordinates fall inside an outer ring (and outside any hole). Every other tool sends `returnGeometry=false`; this is the one request that asks for geometry, simplified server-side via `maxAllowableOffset` (in **degrees**, since `outSR=4326`). Pass `simplify=false` for full precision.

Boundaries are **advisory**, not authoritative property lines — see the designation caveat in the README.

---

## The end-to-end recipe

> *"Which of site 202's designated species were reported inside its boundary in the last 14 days?"*

```
1. get_iba_details(202)
      → centroid (32.7783, -117.2302), suggested_radius_km = 2

2. get_iba_boundary(202)
      → WGS84 rings + bbox

3. get_iba_related_records(202, "species")
      → 8 designated species, each with scientific_name
      → heed the TAXONOMY WARNING and the split notes

4. For each species: resolve scientific_name → current eBird speciesCode
      via ebird__get_taxonomy.        ← NEVER reuse Audubon's species_code

5. ebird__get_nearby_hotspots(centroid, dist=suggested_radius_km)
      → filter to hotspots inside the rings from step 2

6. ebird__get_recent_observations_for_species / per-hotspot observations
```

**Acceptance:** the rail is reported **present** (Kendall-Frost Marsh, Ridgway's Rail via `ridrai1`) — *not* absent, which is what the naive `clarai` chain concludes.

---

## Things not to regress

- **eBird's epistemic guardrails are a feature, not noise.** Low-survey-effort warnings, absence-of-evidence disclaimers, "not yet reviewed" flags, source URLs and retrieval timestamps. They are the model for this whole ecosystem. The problem at the seam is that they can lend *false confidence to a mismatch-induced zero* — the fix is upstream (resolve the code correctly), never weakening the guardrails.
- **Absence of an Audubon related record** means it was never entered, not that the attribute doesn't apply to the site.
- **The `observation` table is historical.** For anything recent or live, go to eBird via the centroid.

## Known gap

There is **no complete species crosswalk**. The server carries a curated table of the known landmines plus a blanket "resolve by name" warning; it does not — and cannot, while remaining auth-free — call eBird's taxonomy API itself to resolve every species. Building a generated crosswalk would mean either shipping a static artifact (crawled from species table `10` and reconciled against eBird taxonomy at build time) or accepting an eBird API key on this server. Until then, **step 4 above is the agent's job, and the warnings exist to make sure it happens.**
