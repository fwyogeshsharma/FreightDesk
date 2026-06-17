# Broker Console — Principal Design Review & Redesign Spec

Scope: refine the existing FastAPI + Postgres broker web app (`webapp/templates/index.html`).
This is an **evolution, not a rewrite**. Current stack: server-rendered Jinja2 + Tailwind
(CDN), IBM Plex fonts, a sortable data table, click-to-call phone chips, server-side
pagination (15/page), auto-hiding LOAD/LOCATION columns.

---

## 0. Aggressive critique of what exists today

The current page is a *competent generic data table*. But a broker's job is not "browse
sightings" — it's **"find a truck I can call RIGHT NOW for a load near X, and dial it in
one tap."** The UI optimizes for the wrong verb. Five hard problems:

1. **Freshness is buried, yet it's the #1 axis.** A 2023 video sighting and a live report
   render identically; "Seen" is just another grey column reading `1173d ago`. Brokers
   will waste calls on dead 3-year-old numbers. Recency/availability must be a *first-class
   visual signal and the default lens* — not a column you scan for.
2. **The money field isn't the hero.** Phone is the entire point of the product, but it
   sits mid-table as one column among nine. It should be the visual anchor and the primary
   CTA, sized and coloured accordingly.
3. **Internal metrics leak to brokers.** The STATUS column shows plate-OCR confidence
   (`Low / High / No plate`). A broker does not care how confident our OCR was about a
   number plate. That's an internal/telecaller metric — it's noise that raises cognitive
   load. Replace with a **Trust** signal (verified vs not) that a broker *does* care about.
4. **Filtering is anemic.** One free-text box (requires a button click) + a source
   dropdown. Brokers think in `{vehicle type, location/route, freshness, availability,
   verified}` — none are quick filters. Search should be instant; the common axes should
   be one-click chips.
5. **No drill-down, no dedup.** Everything is crammed into columns (so empty dashes
   everywhere), and the same truck appears as *thousands of raw sightings*. Brokers want
   **unique trucks (latest sighting each)**, with detail on demand — not a firehose of
   duplicates.

Net: keep the bones (table, Plex, click-to-call, server pagination). Re-rank the
information, add a calling-first layer, and collapse sightings → trucks.

---

## 1. Information hierarchy

**Primary (read in <1s, drives the call):**
- **Phone number** (the CTA)
- **Freshness** (is this truck plausibly available now?)
- **Vehicle type** (does it fit the load?)
- **Location / route**

**Secondary (identity & trust, read on scan):**
- Company name
- Vehicle (plate) number + **Trust** badge (verified / unverified)
- Availability (loaded / unloaded) — once data exists

**Tertiary (detail panel / on demand, never in the default grid):**
- Source (video/image/stream/report), plate-OCR confidence, all phone candidates
  (reported vs OCR), other_text, website, wheels, GPS, review/verification history,
  sighting count & history.

Rule of thumb: **if a broker can't act on it while dialing, it's tertiary.**

---

## 2. Exact screen layout (desktop ≥1280px)

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│ ▣ FreightDesk        ⌘K  Search plate · company · phone …        ● Live   Ops ▾   │  TopBar (sticky)
├──────────────────────────────────────────────────────────────────────────────────┤
│ [ Available ▾ ]  [ Type ▾ ]  [ Location ▾ ]  [ Seen: 24h ▾ ]  [ ✓ Verified ]      │  QuickFilterBar (sticky)
│                                            … 2 filters active · Clear   Advanced ▾  │
├──────────────────────────────────────────────────────────────────────────────────┤
│ 1,284 trucks · Newest first            Sort ▾   Density ▾   Columns ▾   Export ⤓   │  ResultToolbar
├──────────────────────────────────────────────────────────────────────────────────┤
│ ▢  ● TRUCK / PLATE        COMPANY              CALL                TYPE   LOCATION  SEEN   │ header
│ ───────────────────────────────────────────────────────────────────────────────── │
│ ▢  ● RJ14CA1234  ✓Verified  Shree Balaji Trans  ☎ 98110 08120 ⧉   Truck  Jaipur    2h ›│  row (hover → actions)
│ ▢  ● —           ⚠          Mahadev Logistics    ☎ 99280 01122 ⧉   GoodsC  Kota      5h ›│
│ ▢  ● RJ09GB5521  ✓Verified  —                    ☎ 98290 53463 +1   Tanker  —        1d ›│
│                                                  ↳ tap row for full detail            │
├──────────────────────────────────────────────────────────────────────────────────┤
│  ‹  1–25 of ~1,284            rows [ 25 ▾ ]                              25  ›        │  Pager (cursor)
└──────────────────────────────────────────────────────────────────────────────────┘
        click any row ───────────────────────────────► [ right slide-over DetailPanel ]
        select rows  ───────────────────────────────► [ bottom BulkActionBar appears ]
```

- The leading `●` is a **freshness dot** (green <24h / amber <7d / grey older) — the single
  fastest "is this worth calling?" signal.
- **CALL** is the widest, boldest, emerald column. `⧉` = copy. `+1` = more numbers.
- `›` opens the detail slide-over. `⋮`/right-click → row actions.

---

## 3. Component hierarchy (target React)

```
<BrokerConsole>
 ├─ <TopBar>            <Brand/> <CommandSearch ⌘K/> <LiveBadge/> <UserMenu/>
 ├─ <QuickFilterBar>    <FilterChip:Availability/> <FilterChip:Type/> <FilterChip:Location/>
 │                      <FilterChip:Freshness/> <Toggle:Verified/> <ActiveFilters/> <AdvancedButton/>
 ├─ <AdvancedFilterDrawer/>          (slides from right; date range, company, source, wheels, GPS radius…)
 ├─ <ResultToolbar>     <ResultCount/> <SortMenu/> <DensityToggle/> <ColumnPicker/> <ExportMenu/> <SavedViews/>
 ├─ <TruckTable>                      (TanStack Table + Virtual)
 │   ├─ <TableHeader/>   sortable, sticky
 │   └─ <VirtualRows>
 │        └─ <TruckRow>  <Select/> <FreshnessDot/> <PlateCell/> <CompanyCell/> <CallCell/>
 │                       <TypeCell/> <LocationCell/> <SeenCell/> <RowActions/>
 ├─ <Pager/>                          (keyset cursor)
 ├─ <DetailSlideOver/>                (full record, tabs: Overview · Contact · History · Raw)
 └─ <BulkActionBar/>                  (appears on selection: Export, Assign, Mark called)
```

State: **TanStack Query** owns server data (cache, stale-while-revalidate, infinite scroll);
URL owns filters/sort/cursor (shareable, back-button friendly).

---

## 4. Recommended table structure (column triage)

| Column | Disposition | Why |
|---|---|---|
| **Freshness dot** | **Always** (leading) | Fastest availability cue; replaces reading the date. |
| **Plate + Trust badge** | **Always** | Identity + can-I-trust-this in one cell. |
| **Company** | **Always** (truncate + tooltip) | Identity; long names → ellipsis with title tooltip. |
| **CALL (primary phone)** | **Always — hero** | The action. Big tap target, copy, `+N` for extras. |
| **Type** | **Always** | Load-fit filter at a glance. |
| **Location / route** | **Always** (auto-hide if all empty) | Brokers match load origin. |
| **Seen (relative + dot)** | **Always**, right | Recency; exact time in tooltip. |
| Secondary phones | **Inline collapsed** `+N` → popover | Avoid row bloat; still one tap. |
| other_text, website, wheels | **Detail panel** | Long-tail; rarely drives a call. |
| Plate-OCR confidence | **Detail panel** (relabel "extraction quality") | Internal metric, not broker-facing. |
| Source (video/img/stream/report) | **Filter + detail glyph** | Trust/lineage, not a scan column. |
| Verification reason, review history | **Detail → History tab** | Audit, not grid. |
| Loaded/Unloaded | **Always once populated** (badge) + quick filter | Core availability signal later. |

Default grid = **7 columns**, down from 9, with the two highest-value *new* signals
(freshness dot, trust badge) added. Everything else lives in the slide-over.

### TanStack column config (sketch)
```ts
const columns = [
  selectColumn,
  col('freshness', '', { cell: FreshnessDot, size: 28, enableSorting:false }),
  col('plate', 'Vehicle', { cell: PlateTrustCell, sortable:true }),
  col('company', 'Company', { cell: TruncCell, sortable:true, size: 240 }),
  col('phone', 'Call', { cell: CallCell, size: 220, enableSorting:false }), // hero
  col('vehicle_type', 'Type', { cell: TypeCell, sortable:true, size: 120 }),
  col('location', 'Location', { cell: LocCell, sortable:true, hideWhenEmpty:true }),
  col('detected_at', 'Seen', { cell: SeenCell, sortable:true, align:'right', size: 110 }),
  rowActionsColumn,
]
```

---

## 5. Mobile-number treatment (the hero)

- **Dedicated CALL column**, visually dominant: emerald, IBM Plex Mono, formatted
  `98110 08120` (group for readability), min 44px tap height.
- **One-tap dial**: `<a href="tel:+919811008120">` — normalize to E.164 server-side so it
  works on every device.
- **Copy affordance** (`⧉`) on hover/focus — telecallers on desktop softphones copy-paste.
- **Multiple numbers**: show the best/most-frequent first in full; collapse the rest to a
  `+N` pill that opens a small popover (each its own call/copy). Never wrap the row.
- **Called state** (next phase): clicking logs to a `calls` table; the row gets a subtle
  "Called 3m ago by you" tag so the team doesn't double-dial. Enables a "Not yet called"
  quick filter — a big workflow win.
- **Keyboard**: `j/k` move row focus, `c` calls the focused row, `y` copies the number.
  Power telecallers never touch the mouse.

```html
<!-- CallCell -->
<div class="flex items-center gap-2">
  <a href="tel:+919811008120"
     class="inline-flex items-center gap-2 font-mono text-[15px] font-semibold
            text-emerald-700 bg-emerald-50 hover:bg-emerald-100 active:bg-emerald-200
            border border-emerald-200 rounded-lg px-3 py-1.5 min-h-[40px] transition-colors">
    <PhoneIcon class="w-4 h-4"/> 98110 08120
  </a>
  <button class="opacity-0 group-hover:opacity-100 text-slate-400 hover:text-slate-700" title="Copy">⧉</button>
  <button class="text-xs text-slate-500 hover:text-slate-800">+1</button>
</div>
```

---

## 6. Sorting & filtering

**Sort (default ALWAYS Newest first; layer others on top):**
Newest (detected_at desc) · Company A–Z · Type · Location · Trust (verified first) ·
Sighting count · (later) Recently called. Empty values sort last (`NULLS LAST`).

**Quick filters (always-visible chips — the 80% workflow):**
- **Freshness** — `Live · 24h · 7d · 30d · Any` (default **24h** so brokers see callable
  trucks, not history). *Most important filter in the product.*
- **Vehicle type** — multi-select (Truck, Goods carrier, Tanker, Tipper, Container, Bus…).
- **Location / route** — multi-select typeahead (cities/highways).
- **Availability** — `Available (unloaded) · Loaded · Any` (once data flows).
- **Verified only** — toggle.
- (Has-phone is implicit — non-callable video rows are already filtered out.)

**Advanced filters (drawer — the 20%):**
Exact date range · company contains · source (video/image/stream/report) · wheels ·
extraction quality · review status (pending/passed/rejected) · plate text · phone text ·
**near me (GPS radius)** for field reports.

**Saved views:** persist a filter+sort combo ("Unloaded flatbeds · Rajasthan · 24h"). This
is what turns it from a table into a daily SaaS tool.

---

## 7. Scale (100 → 10k → 1M+)

| Records | Strategy |
|---|---|
| **100** | Current server pagination is fine; could even be client-side. |
| **10k** | Server-side filter+sort+paginate (already). **Virtualize** the rendered page so the DOM stays light. Add debounced typeahead search. |
| **1M+** | **(a) Collapse sightings → unique trucks** (one row per normalized phone/plate, with `last_seen` + `sighting_count`) via a materialized view — this alone cuts the working set ~100×. **(b) Keyset/cursor pagination, NOT OFFSET** — deep `OFFSET` is O(n) and dies past a few thousand pages; page by `(detected_at, id) < cursor`. **(c) Full-text search** via Postgres `tsvector` + GIN (and `pg_trgm` for plate/phone fuzzy). **(d) Indexes**: `(detected_at desc, id)`, `vehicle_type`, `city`, `phone_number`, trust. **(e) Approximate counts** ("~1.2M") to avoid `count(*)` on every query. **(f) Infinite scroll with virtualization** as the primary nav, numbered pages secondary. |

**Key product call at scale:** the broker view should be **trucks, not sightings.** Dedup
by contact (normalized phone, then plate) and show the freshest. Sightings/history move to
the detail panel. This is the single biggest usability + performance lever.

---

## 8. Card layout (mobile <768px & the "expanded" form)

```
┌────────────────────────────────────────────┐
│ ● RJ14CA1234   ✓ Verified            2h ago │
│   Shree Balaji Transport                    │
│   Truck · Jaipur · Loaded                   │
│ ┌────────────────────────────────────────┐ │
│ │      ☎  CALL  98110 08120              │ │  ← full-width primary action
│ └────────────────────────────────────────┘ │
│   +1 more number        ⧉ copy     ⋯ more  │
└────────────────────────────────────────────┘
```
- Plate + freshness dot + trust on the top line; relative time right-aligned.
- Company prominent; a single meta line (Type · Location · Availability).
- **Call is a full-width emerald button** — thumb-reachable, the whole point on mobile.
- "more" opens the same detail sheet (bottom sheet on mobile, slide-over on desktop).

---

## 9. Detail slide-over (right panel, desktop) / bottom sheet (mobile)

Tabs: **Overview · Contact · History · Raw**
- **Overview**: plate, company, type, location (+ mini map for GPS reports), availability,
  trust badge + verification reason, source lineage.
- **Contact**: every phone (reported vs OCR, with provenance), call + copy each, website.
- **History**: every sighting of this truck (time, source, location), review/verification
  timeline (who passed/rejected, when, note).
- **Raw**: other_text, plate candidates, extraction quality, frame count — the audit dump.

This is where tertiary data lives, keeping the grid calm.

---

## 10. Empty / loading / error states

- **Loading**: **skeleton rows** (8–10 shimmer rows matching column widths), never a center
  spinner — preserves layout and feels instant. Keep prior data visible while refetching
  (stale-while-revalidate) with a thin top progress bar.
- **Empty — no data yet** (fresh system): illustration + "No trucks yet. Process a video
  (`run.bat --sink db`) or collect field reports." + primary CTA.
- **Empty — no results for filters**: "No trucks match these filters." + the active filter
  chips + **"Clear filters"** + "widen to 7d" suggestion. Distinct from no-data.
- **Error**: inline card with the failure + **Retry**; never a blank screen.
- **Partial/stale**: if showing cached data during a refetch failure, a subtle "Showing
  last loaded results" banner.

---

## 11. Responsive behavior

| Width | Layout |
|---|---|
| **≥1280** | Full 7-col table + slide-over detail. |
| **1024–1280** | Drop Type & Location into a second line under Company; keep Freshness·Plate·Company·CALL·Seen. |
| **768–1024** | Hybrid: condensed rows, CALL stays a button, detail = bottom sheet. |
| **<768** | **Card list** (section 8); filters collapse into a single "Filters" button → full-screen sheet; CALL is full-width. |

Sticky: TopBar + QuickFilterBar + table header all stick; the call column never scrolls out
horizontally (freeze first/contact columns on small screens).

---

## 12. Visual system

- **Type**: keep **IBM Plex Sans** (UI) + **IBM Plex Mono** (plate, phone, timestamps —
  `tabular-nums`). Sizes: 12px meta, 13px body, 15px plate/phone, 11px uppercase labels
  with tracking. This is already a strong, non-generic choice — keep it.
- **Color tokens (semantic, CSS vars):**
  ```css
  --bg:#eef0f4; --surface:#fff; --ink:#0a0e14; --line:#e5e7eb; --muted:#6b7280;
  --go:#059669;     /* call / available / verified  (emerald) */
  --warn:#d97706;   /* unverified / caution         (amber)   */
  --stop:#dc2626;   /* rejected / stale-risk        (red)     */
  --info:#2563eb;   /* loaded / informational       (blue)    */
  --brand:#f59e0b;  /* hi-vis freight accent, sparingly       */
  ```
  Dominant neutral + emerald as the one action colour; amber/red reserved for state. Avoid
  rainbow columns.
- **Density**: Comfortable (52px) default; **Compact (40px)** toggle for power users on big
  monitors. Persist the choice.
- **Badges (small, consistent):** Trust `✓ Verified` (emerald) / `⚠ Unverified` (amber);
  Availability `Available` (emerald-outline) / `Loaded` (blue) / `Unknown` (slate);
  Freshness = a **dot** (green/amber/grey), not text. Review `Passed/Pending/Rejected`
  only on the /review page.
- **Motion**: restrained — 150ms row hover, slide-over 200ms ease-out, skeleton shimmer.
  No bouncy micro-interactions in an all-day ops tool.

---

## 13. Tailwind / React implementation recommendations

**Target (production, scales to 1M):** a small **React + Vite SPA** for the console,
served by the existing FastAPI:
- **TanStack Table v8** (headless) — columns, sorting, selection, column visibility.
- **TanStack Virtual** — row virtualization for 10k+ in the DOM.
- **TanStack Query** — server cache, infinite scroll, stale-while-revalidate.
- **Tailwind** (with a real build + `@tailwindcss/forms`) — drop the CDN for production.
- **Radix UI / Headless UI** — accessible Menu, Dialog (slide-over), Popover, Combobox.
- **lucide-react** — consistent icons (replace inline SVGs).
- URL-driven filters via the router (shareable views, back button).

**API changes to support it:**
- `GET /api/trucks?after=<cursor>&limit=&type=&city=&fresh=24h&verified=&sort=&q=` →
  `{ items, next_cursor, approx_total }` (keyset, not offset).
- `GET /api/meta/filters` → distinct vehicle types, cities (for chip dropdowns).
- A `trucks_unique` materialized view (latest sighting per normalized phone/plate) the grid
  reads; raw sightings stay for the detail History tab.
- Normalize phones to E.164 for `tel:` links.

**Pragmatic bridge (do NOT rewrite now):** the current Jinja2 page is fine to ~10k with
server pagination. High-ROI increments you can ship on it *today* without React:
1. Add the **freshness dot** + default **"Seen: 24h"** filter (biggest workflow win).
2. Replace the plate-confidence STATUS column with the **Trust** badge.
3. Make **CALL** the hero column (bigger, copy button, `+N` popover).
4. Add **quick-filter chips** (Type, Location, Freshness, Verified) — Alpine.js/HTMX for
   instant, no-reload filtering.
5. Add the **row → detail slide-over** (HTMX `hx-get` into a panel).
6. Collapse to **unique trucks** (a SQL view) — fixes scale + dedup immediately.
Then graduate to the React console when live-stream + realtime + 1M rows arrive.

---

## 14. Future-proofing (how later features slot in with no redesign)

The redesign is built around **derived, source-agnostic signals** so new inputs just feed
them:
- **Live video stream** → emits rows with `source=stream`; the **freshness dot** + a global
  **● Live** badge + SSE/WebSocket prepend new rows to the top. No layout change.
- **Image upload / manual broker reports** → new `source` values; the existing
  `/report-test` page becomes "Add truck". They flow into the same grid + Trust badge.
- **Telecaller verification / review** → already powers the **Trust** badge and the
  `/review` queue; add a "Verified" quick filter and a History-tab timeline. Done.
- **Truck availability + Loaded/Unloaded** → already a column (auto-hidden); promote to an
  **Availability badge + quick filter** the moment data exists.
- **Review workflows** → live in the detail panel's History tab + the dedicated /review
  console; the broker grid stays clean.

Because everything maps to four broker-facing abstractions — **Freshness, Trust,
Availability, Contact** — any new pipeline that can populate those slots in without a
redesign.

---

## Implementation status (2026-06-16)

**Shipped** on the Jinja2 broker page (incremental, no rewrite): ① CALL-as-hero (E.164,
copy, +N) · ② Trust badge (replaced plate-confidence) · ③ Freshness dot + recency filter
(default All Time) · ④ Quick-filter chips with HTMX instant-apply (Source/Type/Location/
Freshness/Verified) · ⑤ Row→detail slide-over (HTMX). **Deferred by decision:**
unique-truck collapse (keep raw sightings while validating data); no image storage. **Next
(later):** React + TanStack console, keyset pagination, tsvector search (§7, §13).

## Priority order (what to build first)

1. Default **Freshness filter (24h) + freshness dot** — stops dead-number calls. *(highest ROI)*
2. **Unique-truck collapse** (SQL view) — fixes dedup + scale.
3. **CALL as hero** + copy + `+N`.
4. **Trust badge** replaces plate-confidence STATUS.
5. **Quick-filter chips** (Type, Location, Freshness, Verified) with instant apply.
6. **Detail slide-over**.
7. Graduate to the **React + TanStack** console for 1M-scale + realtime.
