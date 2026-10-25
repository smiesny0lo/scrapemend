# Verification, explained

A repair is only as good as the evidence that it works. scrapemend never trusts a repair because it looks right, or because the model that wrote it is confident. Every repair passes through the same **verification gate** before it can become the active spec, whether a human wrote it or an LLM proposed it.

The gate is strict, but it checks a sample, not every record. Read [what it can and can't catch](#what-the-gate-can-and-cant-catch) before you rely on it.

## Anchors: known-good records

After every complete, good run, scrapemend keeps `heal.anchors` (default 10) records as **anchors**. For each anchor it stores:

- the values of every field,
- the text each value was read from,
- which strategy won,
- the page it came from.

Anchors are chosen by the lowest hash of their key. That makes the choice stable: the same items keep being picked while they exist, so their values stay comparable across runs. It also spreads the anchors across the whole site rather than the first page.

Values are stored in a comparable form: decimals normalized (`566600.00` equals `566600.0`) and dates as ISO strings.

A good run also records, for each field that takes only a few distinct values (a status, a category: up to 20 values, each seen at least three times on average), which values those were.

## The gate

A candidate spec is judged on pages under the **new** layout. Pages the failed run already fetched are reused; the others are fetched politely and stored as snapshots of that run, so the same verdict can be reached again later from the stored pages alone.

1. **Listing.**
   - The item container must find items on the first listing page, and **every** item it matches must have its key: its detail link, or, for a spec without detail pages, its key field. No item may be matched twice. A container that also matches a table's header row fails here.
   - The next-page link is followed for **two pages**, or until it ends if the last good run had fewer pages. Each page must load, pass the same item check, and bring items not seen on the pages before. A link that leads back to an earlier page (a pager whose first link is "Next" on page 1 and "Previous" on page 2) or one hard-coded to page 2 fails here.
2. **Anchors.** On each anchor's page, does every field reproduce the known value? **Every field is judged on its own**, so one wrongly mapped field can't hide behind nine correct ones.
   - *Stable* fields must match exactly on `heal.threshold` of the anchors: by default **all of them**.
   - *Volatile* fields (`volatile: true`, such as prices or statuses) may have changed since the last good run, but most must not: at least `heal.volatile_match` (default 60%) must still match exactly.
   - Anchor pages that fail to load (a notice taken down since) are left out, but at least three must load, or all of them if there are fewer than three.
   - No two fields may read the same text from the same element where their known values differ: one of them is reading the wrong thing. Two fields may read different parts of one element, like a category and a location from "Construction · Dunmere".
   - For a spec without detail pages (no `item.follow`), an anchor's page is only the listing page it was on. The gate finds the anchor's item by its key among the items of the listing pages it has seen, then on the anchor's own listing page.
3. **Sample.** On up to 10 other items of the first listing page (their detail pages, if the spec has them), which no one has known values for:
   - required fields must be filled;
   - a field that is usually filled must not be empty everywhere;
   - a field whose value differs between anchors must not read the same value on every page.
4. **Known values.** A field that took only a few values in the last good run must mostly read values from that set on the anchor and sample pages. A new value now and then is fine; a status that reads "Construction" on every page is not.

A repair is accepted only if there are no problems at all. The report names each problem, for example, for a repair that reads the location from the category's element:

```
location reproduces 0/10 anchor values (needs 100%); expected 'Glenhollow', got 'Consulting'
category and location read the same element on 10 of 10 anchor pages
location reads values the last good run never had on 20 of 20 pages (e.g. 'Consulting'; ...)
```

## Held-out anchors catch overfitting

A repair provider sees only **three** of the anchors as examples: their values, and where those values sat on the old page. The other seven are **held out**.

A strategy that memorised the examples instead of finding where values live in the markup is caught by two checks:

- The held-out anchors fail. A regex matching exactly the three shown titles reproduces only those (and any held-out anchor that happens to share a title).
- The sample check fails, because the strategy finds nothing, or the same thing, everywhere else.

The test suite runs the gate against the demo site with these repairs:

| Repair | Verdict |
|---|---|
| correct repairs for both redesigns, hand-written or Claude's | accepted |
| `location` reading the category element | rejected |
| `deadline` reading the publication date (both are valid dates in the same format) | rejected |
| `status`, a volatile field, reading the category | rejected |
| the next-page link being the pager's first link ("Previous" from page 2 on) | rejected |
| the next-page link hard-coded to page 2 | rejected |
| an item container that also matches the header row | rejected |
| `location` as the last word of the title, wrong only for "Fenwick Vale", when an anchor is such a notice | rejected |
| selectors that match nothing | rejected |
| strategies hard-coding the shown examples' values | rejected |
| a correct repair of a spec without detail pages | accepted |

## What the gate can and can't catch

The gate compares a repair with what it knows: ten anchors, about ten other pages, the first three listing pages, and the values of the last good run. Within that, it is strict. Outside it, it is blind.

**It catches:**

- a field reading the wrong element, if the values differ from the known ones on any anchor (stable fields) or on more than 40% of anchors (volatile fields);
- two fields reading one element where their known values differ;
- a field with a small set of values (a status, a category) reading something else on most checked pages;
- strategies that only fit the examples a provider was shown;
- a required field that is empty on other pages;
- an item container that finds nothing, finds things that aren't items, or finds items twice, on the listing pages it checks;
- a next-page link that loops, stays on one page, or stops early, within the first three pages.

**It can't catch:**

- **A field wrong on only some records, unless one of them is checked.** Only anchors have known values. A repair that reads the location as the last word of the title is wrong only for places with two words: in the demo, 12 notices of 200. Ten anchors picked from 200 miss all 12 about **53%** of the time, and then the repair passes. A field wrong on 2 records in 200 passes about 90% of the time. More anchors (`heal.anchors`) narrow this, but no sample closes it. Watch the first runs after a promotion.
- **Plausible values on pages without known values.** The sample check catches empty and constant values, not a wrong title that looks like a title.
- **Pagination that breaks later.** Only the first three listing pages are checked; a next-page link that fails on page 7 isn't seen.
- **Volatile fields wrong on a minority of anchors.** Up to 40% of anchors may differ by default, because prices and statuses do change. A volatile field mapped to something that agrees with it on most records passes.
- **Custom strategies sharing an element.** The shared-element check needs to know which element a strategy read. Built-in strategies can say; a custom strategy can only if it has a `locate(doc)` method.

**It can reject a correct repair** when the data really changed: a stable field corrected on an anchor page since the last good run, more than 40% of a volatile field's values changed, or a small set of values replaced wholesale (statuses renamed). If that happens, look at the report, then lower `heal.threshold` or `heal.volatile_match`, mark the field `volatile`, or run the spec once with a version that works to refresh the anchors.

## Then what?

An accepted repair is stored as a proposal. Under `heal.mode: auto`, it is promoted to a new spec version only when all three of these hold:

- the failure carried **structural evidence**: the page fingerprints changed, `layout_changed`;
- the repair passed;
- the repair's confidence is at least 0.5.

A `partial` failure (one field broken on an unchanged page) is never promoted automatically: the site may have legitimately stopped showing that field. In `propose` mode, every accepted repair waits for:

```sh
scrapemend diff SPEC ID      # review it
scrapemend promote SPEC ID   # accept it
```

If no repair passes, the source is quarantined, and a `repair_rejected` or `quarantined` event goes to the notifiers you configured with `--notify`. The rejected proposals stay stored, so a human can see what was tried and why it failed.

`blocked`, `transient` and `drift` are never repaired: the site refused or failed us, or real-world data changed. That isn't something a selector should fix.
