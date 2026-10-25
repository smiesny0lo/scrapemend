# Verification, explained

A repair is only as good as the evidence that it works. scrapemend never trusts a repair because it looks right, or because the model that wrote it is confident. Every repair passes through the same **verification gate** before it can become the active spec, whether a human wrote it or an LLM proposed it.

## Anchors: known-good records

After every complete, good run, scrapemend keeps `heal.anchors` (default 10) records as **anchors**. For each anchor it stores:

- the values of every field,
- the text each value was read from,
- which strategy won,
- the page it came from.

Anchors are chosen by the lowest hash of their key. That makes the choice stable: the same items keep being picked while they exist, so their values stay comparable across runs. It also spreads the anchors across the whole site rather than the first page.

Values are stored in a comparable form: decimals normalized (`566600.00` equals `566600.0`) and dates as ISO strings.

## The gate

A candidate spec is judged on pages under the **new** layout. Pages the failed run already fetched are reused; the others are fetched politely and stored as snapshots, so the verdict can be reproduced later.

1. **Listing.** Does the item container find items on the listing page? Do at least 80% of them link to a detail page? Is there still a next-page link, if the last good run had more than one page?
2. **Anchors.** On each anchor's page, does every field reproduce the known value?
   - *Stable* fields must match exactly.
   - *Volatile* fields (`volatile: true`, such as prices or statuses) only need to be present where they were present, and absent where they were absent.
   - **Every field must reach `heal.threshold` (default 90%) on its own**, so one wrongly mapped field can't hide behind nine correct ones. At least three anchor pages must load.
3. **Sample.** On up to 10 other detail pages, which no one has known values for:
   - required fields must be filled;
   - a field that is usually filled must not be empty everywhere;
   - a field whose value differs between anchors must not read the same value on every page.

A repair is accepted only if there are no problems at all. The report names each problem, for example:

```
location reproduces 0/10 anchor values (needs 90%); expected 'Glenhollow', got 'Consulting'
```

## Held-out anchors catch overfitting

A repair provider sees only **three** of the anchors as examples: their values, and where those values sat on the old page. The other seven are **held out**.

A strategy that memorised the examples instead of finding where values live in the markup is caught by two checks:

- The held-out anchors fail. A regex matching exactly the three shown titles reproduces 3 of 10.
- The sample check fails, because the strategy finds nothing, or the same thing, everywhere else.

The test suite proves this on the demo site. Its verification tests hand the gate these repairs and check every verdict:

| Repair | Verdict |
|---|---|
| correct repairs for both redesigns | accepted |
| `location` reading the category element | rejected |
| `deadline` reading the publication date (both are valid dates in the same format) | rejected |
| selectors that match nothing | rejected |
| strategies hard-coding the shown examples' values | rejected |

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

If no repair passes, the source is quarantined and an alert goes out. The rejected proposals stay stored, so a human can see what was tried and why it failed.

`blocked`, `transient` and `drift` are never repaired: the site refused or failed us, or real-world data changed. That isn't something a selector should fix.
