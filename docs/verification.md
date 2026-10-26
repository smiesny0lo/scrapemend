# Verification, explained

A repair is only as good as the evidence that it works. scrapemend never trusts a repair because it looks right, or because the model that wrote it is confident. Every repair passes through the same **verification gate** before it can become the active spec, whether a human wrote it or an LLM proposed it, and whether it is promoted by hand or automatically.

The primary check is a **shadow run**: the repaired spec crawls the source, and its records are compared, by key, with the last good run's. A few quicker checks come first, so most wrong repairs are turned down without a crawl. Read [what the gate guarantees](#what-the-gate-guarantees) and [what it can't catch](#what-it-cant-catch) before you rely on it.

## Anchors and known records

After every complete, good run, scrapemend keeps two things:

- **The values of every valid record**, by key, in a comparable form: decimals normalized (`566600.00` equals `566600.0`), dates as ISO strings. These are what the shadow run is compared with.
- **`heal.anchors` (default 10) anchors**: records the gate can fetch again quickly. For each, the values of every field, the text each value was read from, which strategy won, and the page it came from.

Anchors are the records whose keys come first in a fixed random order: a hash of a fixed seed and the key. A URL's scheme and host (and port) are left out of the hash, so the same site served from another address keeps the same anchors. The choice is stable, so the same items keep being picked while they exist, and spread over the whole site rather than the first page.

A good run also records, for each field that takes only a few distinct values (a status, a category: up to 20 values, each seen at least three times on average), which values those were, and the listing pages it read with the number of items on each.

## The gate

A candidate spec is judged on pages under the **new** layout. Pages the failed run already fetched are reused; the others are fetched politely (the spec's rate limit, robots.txt, the same User-Agent) and stored as snapshots of that run. So the same verdict can be reached again later from the stored pages alone, and `promote` usually fetches nothing.

Every check ends **passed**, **failed** or **not run**, and the report lists each one with what it covered, or why it didn't run:

```
proposal 2 (patch:v2-status-enumerates-examples.yaml, confidence 0.90): failed
  passed   regexes: 2 regexes: 2 on Python's re (at most 1s a search)
  passed   listing: 20 items on the first listing page, 3 listing pages along the next-page link
  passed   anchors: 10 of 10 anchor pages compared; weakest field 60%
  passed   sample: 10 other pages
  passed   known values: category, location, status on 20 pages
  failed   shadow run: the whole source: 10 listing pages, 200 records; all 200 of the last good run's records compared
    problem: status became empty on 39 of 200 records that had a value in the last good run (a volatile field may change, but not become empty); expected 'Awarded', got None
```

That repair reads `status` with a regex that only knows the values of the examples a provider is shown, `^(Open|Closed)$`. It is right on six of the ten anchors, which is enough for a volatile field there, and wrong on every Awarded notice: the shadow run sees all 39.

### The quick checks

1. **Regexes.** No regex may take longer than its engine allows, on any page the gate reads (see [Regexes](#regexes)). A provider's regex that RE2 can't compile fails here, before anything else runs.
2. **Listing.**
   - The item container must find items on the first listing page, and **every** item it matches must have its key: its detail link, or, for a spec without detail pages, its key field. No item may be matched twice. A container that also matches a table's header row fails here.
   - The next-page link is followed for **two pages**, or until it ends if the last good run had fewer pages. Each page must load, pass the same item check, and bring items not seen on the pages before. A link that leads back to an earlier page, or one hard-coded to page 2, fails here.
3. **Anchors.** On each anchor's page, does every field reproduce the known value? Every field is judged on its own, so one wrongly mapped field can't hide behind nine correct ones.
   - *Stable* fields must match on `heal.threshold` of the anchors: by default **all of them**.
   - *Volatile* fields (`volatile: true`, such as prices or statuses) must match on at least `heal.volatile_match` (default 60%) of them: their values may have changed since the last good run.
   - No two fields may read the same text from the same element where their known values differ: one of them reads the wrong thing.
   - Anchor pages that fail to load (a notice taken down since) are left out, but at least three must load, or all of them if there are fewer. For a spec without detail pages, an anchor is found by its key among the items of the listing pages.
4. **Sample.** On up to 10 other items of the first listing page: required fields must be filled, a field that is usually filled must not be empty everywhere, and a field whose value differs between anchors must not read the same value on every page.
5. **Known values.** A field that took only a few values in the last good run must mostly read values from that set on the anchor and sample pages.

If a quick check fails, the shadow run is skipped: the repair is rejected anyway.

### The shadow run

The candidate crawls the source as a run would: every listing page along **its** next-page link (up to `fetch.paginate.max_pages`), every item its container matches, every detail page its follow link points to. Then its records are compared with the last good run's, by key:

- **Coverage.** It must find at least **90%** of the records the last good run had. A container that misses items, a follow link that reads the wrong link, or pagination that stops early fails here.
- **Stable fields** must be **identical** on every record both runs have (on `heal.threshold` of them: by default all).
- **Volatile fields** may have changed on at most **`heal.volatile_change`** (default 10%) of those records. A value that became empty is never a change: it is a mismatch, judged like a stable field's, so by default none may.
- On records the last good run didn't have, **required fields** must be filled. A field with a small set of values must mostly read values from that set.

The comparison needs at least 20 records both runs have (or all of them, if the last good run had fewer).

When the last good run had more records than **`heal.shadow_max_items`** (default 500), the shadow run reads a sample of that many. It draws listing pages at random, with a fixed seed, from the last good run's (twice as many as the cap needs), and checks each one's item count against the last good run's count for the same page. From their items it draws up to the cap, again with a fixed seed. At least 90% of the records drawn must be known from the last good run. The same pages and records are drawn every time, so a verdict can be reached again. `shadow_max_items: all` reads the whole source instead, however large.

Before the shadow run starts, `heal` (and `promote`) say what it will read, and about how many requests that takes at the spec's rate limit: one per page that isn't stored yet. A crawl you can't afford now can be stopped with Ctrl-C.

```
heal: shadow run: the whole source, all 200 of the last good run's records: about 187 requests (10 listing pages and 200 detail pages, 23 of them stored), at least 9s at 20 requests a second
```

### Fail closed

A repair is **accepted** only if every check passed. One that failed a check is **rejected**. One where a check couldn't run is **unverified**, never accepted. A check doesn't run when, for example:

- a page couldn't be fetched (a timeout or a 5xx after every retry), or an old listing page a sample needs is gone;
- the last good run's records aren't stored (a database from before they were kept), or fewer than 20 can be compared;
- there are no anchors, or too few anchor pages load;
- the failed run's listing page isn't in the snapshots (`promote` pointed at the wrong `--snapshots`);
- a quicker check failed first (the shadow run is skipped).

A shadow run that stops at a page it can't fetch still fails the repair if the records it did compare already break the rules, whatever the rest would read: then `--force` can't help.

`heal` keeps an unverified repair, quarantines the source and says why. It doesn't ask the provider for another attempt: a new patch can't make a page load.

```
heal: quarantined: proposal 1 could not be verified: shadow run: the last good run's records are not stored; review it, then promote it with --force, or run heal again
```

`scrapemend promote` verifies it again first. A page that didn't load before is fetched again, so promoting later may pass without anything else. If a check still can't run, only `--force` promotes it, and never past a check that failed. **Automatic promotion never uses `--force`.**

```
$ scrapemend promote examples/sources/demo.yaml 1
proposal 1 could not be verified again: shadow run: the last good run's records are not stored; pass --force to promote it anyway (never past a check that failed)
$ scrapemend promote examples/sources/demo.yaml 1 --force
tenders-demo: proposal 1 verified again; promoted with --force although these checks did not run: shadow run (the last good run's records are not stored); v2 is now active
```

The new version's note says which checks it was forced past.

## Regexes

A regex runs on page text, which the scraped site controls, and a pattern that backtracks catastrophically can run for hours on the wrong text. So every regex strategy runs on one of two engines (`engine:` in the spec):

- **`re2`** runs in time linear in the text: no text can make it slow. **Every regex of a repair proposed by a provider runs on RE2**, and is stored that way. A regex RE2 can't compile (a backreference, a lookaround) is rejected. RE2's `\w`, `\d` and `\s` match ASCII only.
- **`re`**, Python's own, is the default for specs and patches you write: they keep Python's semantics. Each search runs in a worker process and is stopped after **1 second**. A strategy that times out counts as finding nothing, so the field's next strategy gets its turn, and after three timeouts in a run it isn't tried again. During verification, any timeout fails the regexes check.

`pattern` (a field's validation regex) also runs on Python's re with the same limit.

## Held-out anchors, and fitting the examples

A repair provider sees only **three** of the anchors as examples: their values, and where those values sat on the old page. The other seven are held out. A strategy that memorised the shown values fails on the held-out anchors, and the sample check fails it too: it finds nothing, or the same thing, on every other page.

A strategy fitted to the examples in a subtler way is right on most records, and the anchors alone don't catch it: ten records rarely include the ones it gets wrong. The shadow run does, because it reads them all:

| Repair (review attack) | Wrong on | Anchors | Shadow run |
|---|---|---|---|
| `value` reads exactly five digits, like the first example (A3) | 102 of 200 | may pass: 60% is enough for a volatile field | `value changed on 102 of 200 records` |
| `value` reads at most six digits: every amount over €1M loses its millions (S1) | 29 of 200 | pass with up to 4 anchors over €1M | `value changed on 29 of 200 records` |
| `status` only knows Open and Closed, the examples' values (S2) | 39 of 200 empty | pass with up to 4 Awarded anchors | `status became empty on 39 of 200 records` |
| `location` is the title's last word: Fenwick Vale reads "Vale" (S3) | 12 of 200 | pass in about half of all draws of ten anchors | `location reproduces 188/200 values of the last good run` |

The test suite verifies each of these with ten anchors that all fit the repair, and the last one with ten random draws of anchors: all are rejected.

The suite also runs the gate against the demo site with these repairs:

| Repair | Verdict |
|---|---|
| correct repairs for both redesigns, hand-written or Claude's, and one spec healed through both (v1 → v2 → v3) | accepted |
| `location` reading the category element | rejected |
| `deadline` reading the publication date, `published` reading the deadline (both valid dates in the same format) | rejected |
| `status`, a volatile field, reading the category | rejected |
| `description` reading another paragraph | rejected |
| the next-page link being the pager's first link ("Previous" from page 2 on), or hard-coded to page 2 | rejected |
| an item container that also matches the header row | rejected |
| selectors that match nothing, or strategies hard-coding the shown examples' values | rejected |
| a hand-written regex that backtracks catastrophically | rejected in seconds |
| the same regex from a provider (run on RE2, where it can't backtrack) | accepted: it never matches, and the fallback after it is right |
| a provider's regex with a lookahead | rejected: RE2 can't compile it |
| a correct repair whose shadow run can't run (no stored records, a page that won't load) | unverified: only `promote --force` |
| a correct repair of a spec without detail pages | accepted |

## What the gate guarantees

When a repair is accepted with the default settings and a full shadow run (a source of up to 500 records, or `heal.shadow_max_items: all`), then, on the pages it read under the new layout (the whole source along its next-page link, up to `max_pages`):

- it found at least 90% of the records the last good run had;
- on every record both runs have, every **stable** field reads exactly the last good run's value;
- on those records, a **volatile** field reads another value on at most 10% of them, and no value it had became empty;
- required fields are filled on records the last good run didn't have, and fields with a small set of values mostly read values from that set;
- the quick checks passed: listing, anchors, sample, known values, and no regex timed out.

The same checks run whether the repair is promoted by `scrapemend promote` or automatically, on the same pages.

## What it can't catch

- **Records the last good run didn't have.** Only their required fields and small value sets are checked. A plausible but wrong value on a new record passes.
- **Records the shadow run didn't find.** Up to 10% of the last good run's records may be missing (removed from the site, or missed by the repair), and those aren't compared.
- **Volatile fields wrong on a few records.** Up to 10% may read another value, because prices and statuses do change. A volatile field that is wrong on fewer than 10% of records, without becoming empty, passes.
- **A sample, for a source with more records than `heal.shadow_max_items` (default 500).** Only the records drawn are compared. A stable field wrong on a share *p* of records is caught with probability 1 − (1 − *p*)ⁿ for *n* records drawn. With the default 500, a field wrong on 1% of records is caught 99.3% of the time, on 0.5% about 92%, and on 0.1% only about 39%; the last-word location (12 of 200) with 50 records drawn, about 95% of the time. A volatile field's changed share is estimated from the sample. Set `all` where a field wrong on a few records matters more than the requests.
- **Changes after the verification.** `promote` verifies again on the pages `heal` stored, not on the site as it is now. A redesign in between isn't seen until the next run.
- **Pagination past `max_pages`**, and **custom strategies sharing an element**: the shared-element check needs to know which element a strategy read, which a custom strategy only says if it has a `locate(doc)` method.

**After promotion**, the runs that follow are compared with the last good run too: a field whose empty rate moves by more than 5 points, a common value of a small set (5% of records or more) that disappears, or numbers that change magnitude make the run `drift`, which alerts. A drift run doesn't become the last good run, so every run after it drifts and alerts too, until a human accepts one with `scrapemend accept-drift` (see [drift](running.md#drift)). Watch the first runs after a promotion anyway.

**It can reject a correct repair** when the data really changed: a stable field corrected since the last good run on any record, more than 10% of a volatile field's values changed, more than 10% of the records gone, or a small set of values replaced wholesale (statuses renamed). `--force` can't help there: the check failed. Look at the report, then mark the field `volatile`, or loosen `heal.threshold` or `heal.volatile_change`, in a new version of the spec file, and run it again. If you have checked the repair yourself, you can also write it into that new version: a spec file you edit is yours, not a repair, and isn't verified.

## Then what?

An accepted repair is stored as a proposal. Under `heal.mode: auto`, it is promoted to a new spec version only when all three of these hold:

- the failure carried **structural evidence**: the page fingerprints changed, `layout_changed`;
- the repair passed every check;
- the repair's confidence is at least 0.5.

Before it is promoted, it is verified again exactly as `scrapemend promote` would, and it is not promoted if that doesn't pass. A `partial` failure (one field broken on an unchanged page) is never promoted automatically: the site may have legitimately stopped showing that field. In `propose` mode, every accepted repair waits for:

```sh
scrapemend diff SPEC ID      # review it
scrapemend promote SPEC ID   # accept it
```

`promote` runs the gate again before anything changes, against the current anchors and the failed run's stored pages (pass the same `--snapshots` as the run). The anchors may have been refreshed since, or the proposal accepted by an older version of the gate; one that no longer passes is marked rejected and not promoted (exit code 3). A proposal made for another spec version than the active one is refused; `--force` applies it to the active version instead, and verifies that.

If no repair passes, the source is quarantined, and a `repair_rejected` or `quarantined` event goes to the notifiers you configured with `--notify`. The rejected and unverified proposals stay stored, so a human can see what was tried and why it failed.

`blocked`, `transient` and `drift` are never repaired: the site refused or failed us, or real-world data changed. That isn't something a selector should fix.
