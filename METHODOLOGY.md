# ABCO Catalog Rebuild — Methodology

This document defines the data model, enrichment rules, and the controlled
vocabularies the pipeline uses. Read it before changing any script.

---

## The business problem

ABCO distributes plating **chemicals, supplies, and equipment**. The catalog is
the foundation of all analytics, reporting, and (eventually) product
recommendations — matching what a customer orders back to the plating *lines*
running in their shop. To do that we need each catalog item enriched with: what
it is, what process step it serves, which plating lines it belongs to, and what
it is used alongside.

We already have a 35-column catalog (`data/inputs/catalog_list.csv`) with the
right columns but unreliable attribute values. This pipeline rebuilds the
attributes cleanly and repeatably from the Technical Data Sheets (TDS) in
`data/inputs/tds_ready/`.

---

## The spine: what we trust vs. what we rebuild

The input catalog is the **spine**. These columns are trusted and preserved
verbatim — the pipeline never changes them:

| Column | Meaning |
|---|---|
| `Item` | Canonical SKU name. The join key. |
| `Inventory Type` | System / Misc Resale / Misc Retail / Price List / Quote Required / Inventoried |
| `status` | `active` (current SKU) or `legacy` (superseded) |
| `superseded_by` | For a legacy row, the active `Item` that replaced it |
| `consolidation_group` | Pre-existing consolidation grouping |
| `Old Item Name` | Prior name a row consolidated |
| `item_category` | `Chemistry` / `Supplies` / `Equipment` — **trusted as-is, used to route enrichment** |

Everything else is an **attribute we rebuild**: `Brand`, `is_commodity`,
`product_category`, `function_description`, `applicable_lines`,
`primary_line_position`, `primary_line_sub_position`, `secondary_line_position`,
`secondary_line_sub_position`, `use_type`, `system_name`, `system_components`,
`sequence_companions`, `replenishment_role`, `application_method`,
`typical_concentration`, `manufacturer`, `manufacturer_product_code`, `size`,
`canonical_product_name`, `tds_filename`, `match_tier`, `match_score`,
`match_basis`.

The marketing columns (`Public Catalog Description`, `SEO Title`,
`SEO Description`) are **left untouched** in the current round.

---

## Four rules everything follows from

1. **The catalog drives TDS selection — never the reverse.** For each catalog
   product we look for a matching TDS. We never create a catalog row because a
   TDS exists. No products are added or dropped.

2. **The active item is the unit of enrichment; information flows backward.**
   We enrich each active item once. A legacy item inherits its enrichment from
   the active item named in `superseded_by`. An orphan legacy item (no active
   match) is enriched on its own.

3. **Priority = all chemistry + active supplies/equipment.** Legacy
   supplies/equipment are deprioritized: they inherit from an active match if one
   exists, and otherwise get only minimal treatment — no rework.

4. **Correctness over coverage, and runs are repeatable.** Low-confidence
   matches go to a review file rather than being silently merged. Every API
   stage caches to disk, so re-running on unchanged inputs makes zero API calls
   and reproduces byte-identical output.

---

## Enrichment routing (by trusted `item_category`)

| Item kind | Path | Source |
|---|---|---|
| Chemistry **with** a confirmed TDS | TDS extraction | native PDF → Claude, full schema |
| Chemistry **without** a TDS (commodities, no match) | inference | name + brand + domain knowledge; `is_commodity` set where apt |
| Supplies / Equipment (no TDS exists) | inference | name + domain knowledge; only the columns that legitimately apply |

For supplies/equipment, the chemistry-only columns stay blank:
`typical_concentration`, `system_components`, `sequence_companions`,
`is_commodity`, `manufacturer_product_code`, `tds_filename`. What we *do* infer
is a short `function_description`, a `product_category`, and `applicable_lines`
(e.g. aluminum racking wire → anodize lines; a zinc anode → zinc lines), plus
`size` / `canonical_product_name` parsed from the item name.

---

## Provenance: `match_tier`, `match_score`, `match_basis`

Every enriched row records where its data came from so it is auditable and
reviewable:

| `match_basis` | Meaning |
|---|---|
| `product_code:<code>` | TDS confirmed by a literal manufacturer product-code hit (tier 1) |
| `tds_verified` | TDS confirmed by the Claude verification pass (tier 1) |
| `tds_verified_low_conf` | TDS verified but below the auto-accept bar (tier 2/3 — review) |
| `manual` | TDS linked by hand in `data/inputs/manual_matches.csv` (tier 1) |
| `llm_inference` | No TDS — attributes inferred from the name + domain knowledge |
| `inherited_from_active` | Copied backward from the `superseded_by` active item |

`match_score` is 0–100 confidence. `match_tier`: 1 = high-confidence /
auto-applied; 2–3 = needs review (written to `catalog_review.csv`); 4 = inferred.

---

## Controlled vocabularies

These are the single source of truth (defined in `scripts/lib.py`). Extraction
and inference must choose from these exact strings.

### `applicable_lines`
```
Zinc (pure), Zinc-nickel, Zinc-iron,
Electrolytic nickel, Electroless nickel,
Electrolytic copper, Electroless copper,
Decorative chrome, Hard chrome,
Tin, Tin-copper, Tin-silver, Tin-bismuth,
Gold, Silver, Palladium, Palladium-nickel, Platinum, Rhodium,
Cobalt, Iron, Indium, Cadmium,
Anodize Type II, Anodize Type III,
Chromate Conversion
```

### `primary_line_position` (process stage)
```
1. Pre-treatment / Cleaning
2. Rinse
3. Pre-plate / Strike
4. Plating (Main Bath)
5. Post-plate Treatment
6. Final Rinse & Dry
```

### `primary_line_sub_position`
```
1.1 Mechanical Surface Prep   1.2 Solvent / Degreasing   1.3 Soak Cleaning
1.4 Electrocleaning           1.5 Acid Activation / Pickling   1.6 Etch
1.7 Desmut / Deoxidize        1.8 Additives
2.1 Rinse Water   2.2 Rinse Tank Configurations   2.3 Rinse Aids / Additives
3.1 Copper Strike   3.2 Nickel Strike   3.3 Acid Dip / Pre-dip
3.4 Specialty Strikes   3.5 Adhesion Promoters
4.1 Electroless Plating   4.2 Electrolytic Plating — Nickel
4.3 Electrolytic Plating — Copper   4.4 Electrolytic Plating — Zinc
4.5 Electrolytic Plating — Chrome   4.6 Electrolytic Plating — Precious Metals
4.7 Electrolytic Plating — Tin & Alloys   4.8 Anodizing
4.9 Bath Additives & Replenishers
5.1 Chromate Conversion Coatings   5.2 Passivation   5.3 Phosphate Coatings
5.4 Sealers   5.5 Topcoats / Final Coatings   5.6 Specialty Post-treatments
6.1 Final Rinse   6.2 Drying Methods   6.3 Final Rinse Additives
```

`secondary_line_position` / `secondary_line_sub_position` use the same lists, and
are only set when a product genuinely serves a second distinct stage.

### Enumerated attribute values
- `use_type`: `standalone` | `system_component`
- `replenishment_role`: `makeup_only` | `replenishment_only` | `both` | `not_applicable`
- `application_method`: `immersion` | `electroplating` | `barrel` | `spray` | `brush_swab` | `multiple`

---

## Pipeline stages

| Script | API? | What it does |
|---|---|---|
| `00_validate_inputs.py` | no | Validate the spine; fail loud on structural problems. |
| `01_index_tds.py` | no | Index every PDF in `tds_ready/` (text + metadata) for matching. |
| `02_match_catalog_to_tds.py` | yes | Match active **chemistry** items to a TDS (fuzzy + code shortcut + Claude verification). |
| `03_enrich.py` | yes | TDS extraction for matched chemistry; inference for everything else; fill `Brand`. |
| `04_assemble.py` | no | Inject active enrichment, propagate **backward** to legacy items, write final catalog. |
| `05_validate_output.py` | no | Fail-loud checks on the final catalog (row conservation, vocab, integrity). |

See `README.md` for how to run each stage.
