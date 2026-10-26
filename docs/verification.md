# Verification, explained

A repair is only as good as the evidence that it works. specmend never trusts a repair because it looks right, or because the model that wrote it is confident. Every repair passes through the same **verification gate** before it can become the active spec, whether a human wrote it or an LLM proposed it, and whether it is promoted by hand or automatically.

The primary check is a **shadow run**: the repaired spec crawls the source, and its records are compared, by key, with the last good run's. A few quicker checks come first, so most wrong repairs are turned down without a crawl. Read [what the gate guarantees](#what-the-gate-guarantees) and [what it can't catch](#what-it-cant-catch) before you rely on it.

## Anchors and known records

After every complete, good run, specmend keeps two things:

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
  passed   anchors: 10 of 10 anchor pages compared; weakest field 70%
  passed   sample: 10 other pages
  passed   known values: category, location, status on 20 pages
  failed   shadow run: the whole source: 10 listing pages, 200 records; all 200 of the last good run's records compared
    problem: status became empty on 39 of 200 records that had a value in the last good run (a volatile field may change, but not become empty); expected 'Awarded', got None
  fields, on the 200 records of the last good run it compared:
    reference: 200 compared, 0 changed, 0 emptied (stable: none may differ): none
    ...
    value: 200 compared, 0 changed, 0 emptied (volatile: up to 20 may change, none may become empty): none
    status: 200 compared, 0 changed, 39 emptied (volatile: up to 20 may change, none may become empty): more than allowed
    description: 200 compared, 0 changed, 0 emptied (stable: none may differ): none
  records: found 200 of the last good run's 200 (at least 180 needed)
  judged on their own: every value of category (7), location (16) and status (3); the records where value was empty; the 10 largest and the 10 smallest values of value
```

That repair reads `status` with a regex that only knows two of its three values, `^(Open|Closed)$`, as if fitted to examples that showed no other. It is right on seven of the ten anchors, which is enough for a volatile field there, and wrong on every Awarded notice: the shadow run sees all 39.

After the checks, every report says what was compared, field by field: how many values changed, how many became empty, what the rules allow, and what they tolerated. That holds whether the repair passed, failed or is about to be forced. It is the shadow run's comparison if it compared any records, the anchors' otherwise. Then come the records it found, how many were new since the last good run (only their required fields and small sets of values are checked), and what it judged on its own. A report that passed says what it let through, so whoever promotes the repair can see it:

```
    value: 200 compared, 5 changed, 0 emptied (volatile: up to 20 may change, none may become empty): tolerated 5
  new: 20 records the last good run didn't have: only their required fields, and fields with a small set of values, were checked
```

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

The candidate crawls the source as a run would: every listing page along **its** next-page link (up to `fetch.paginate.max_pages`), every item its container matches, every detail page its follow link points to (on a large source, a sample of them: see below). Then its records are compared with the last good run's, by key:

- **Coverage.** It must find at least **`heal.shadow_min_found`** (default **90%**) of the records the last good run had. A container that misses items, a follow link that reads the wrong link, or pagination that stops early fails here. A source that loses records by itself may need less: see [sources that churn](#sources-that-churn).
- **Coverage of every kind of record.** It must find that share of each kind of record too: of the records where a field was empty, and of the records of each value of a field with a small set of values. A container that skips one kind of item skips all of them, in every run. A kind that falls short counts only if chance can't explain it: if it would happen less than once in a thousand times, were each record as likely to be gone as the source's records overall. One of three Cleaning notices gone, where a tenth of all notices are, is churn. None of the 9 notices without an amount found, where every other notice was, is not:

  ```
  problem: the shadow run found 0 of the 9 records of the last good run whose value was empty (needs 90%), against 191 of the other 191: the item container, the detail links or the pagination miss that kind of record
  ```
- **Stable fields** must be **identical** on every record both runs have (on `heal.threshold` of them: by default all).
- **Volatile fields** may have changed on at most **`heal.volatile_change`** (default 10%) of those records. A value that became empty is never a change: it is a mismatch, judged like a stable field's, so by default none may.
- On records the last good run didn't have, **required fields** must be filled. A field with a small set of values must mostly read values from that set.
- **The largest and the smallest values.** A number field is judged on its own on the 10 records where it was largest in the last good run, and on the 10 where it was smallest: it may change there no more often than the rules allow, unless chance can put that many changes there (more often than once in a thousand times, given how often it changed overall). Prices that really change don't pick the largest; a pattern too narrow for long or short numbers misreads exactly those. A `value` that loses the millions of every amount from €2M up changes on 5 of the demo's 200 records, within the 10% a volatile field may change by, but on 5 of its 10 largest:

  ```
  problem: value changed on 5 of the 10 records where it was largest in the last good run, against 0 of the other 190: a strategy may misread large numbers; expected '2412600', got '412600'
  ```

The comparison needs at least 20 records both runs have (or all of them, if the last good run had fewer).

When the last good run had more records than **`heal.shadow_max_items`** (default 500), the shadow run samples detail pages, never listing pages. It still reads every listing page along the candidate's next-page link, as a run would (about one request per page of items), and must find `heal.shadow_min_found` (90%) of the items the last good run found on them: a next-page link that stops at page 10 of 60 fails here. From the items of every page it draws `shadow_max_items` to read the detail pages of: first the records a rule judges on their own, the 10 largest and the 10 smallest values of each number field, and a record of every value of each field with a small set of values (a status, a category, a place); then others at random, with a fixed seed. So a field wrong only on the notices of one place is caught by every sample, because every sample reads a notice of every place. These records are read even past the cap. At least `heal.shadow_min_found` of the records drawn must be known from the last good run. The same records are drawn every time, so a verdict can be reached again. `shadow_max_items: all` reads every detail page instead, however many. A spec without detail pages is always read whole: its records are on the listing pages the shadow run reads anyway.

Before the shadow run starts, `heal` (and `promote`) say what it will read, and about how many requests that takes at the spec's rate limit: one per page that isn't stored yet. A crawl you can't afford now can be stopped with Ctrl-C.

```
heal: shadow run: the whole source, all 200 of the last good run's records: about 187 requests (10 listing pages and 200 detail pages, 23 of them stored), at least 9s at 20 requests a second
```

### Sources that churn

The shadow run is compared with the last good run, and that run may be days old by the time a repair is verified: the site broke on a Friday and `heal` ran on Monday, or the source sat quarantined for a week. On a source whose records come and go by themselves (tenders that close and come down, job ads, listings that sell), more of the last good run's records than 10% may be gone by then, and a correct repair is rejected for coverage:

```
problem: the shadow run found 172 of the last good run's 200 records (needs 90%): the item container, the detail links or the pagination miss some
```

Such a source may lower **`heal.shadow_min_found`**. Pick a share below what survives the longest gap you expect between a good run and a heal: a source that loses 5% of its records a day keeps about 86% over three days (0.95³), so 0.8 leaves room. Measure the churn from two good runs' records rather than guessing.

The price is what a lower share lets through: a repair that misses that many records (pagination that stops early) passes coverage too. One that misses one kind of record still fails, unless that kind loses no more than chance would take. Records the shadow run doesn't find are never compared, so the rules for the others don't change, and `expect.min_items_per_run` still judges every run after the promotion.

### Fail closed

A repair is **accepted** only if every check passed. One that failed a check is **rejected**. One where a check couldn't run is **unverified**, never accepted. A check doesn't run when, for example:

- a page couldn't be fetched (a timeout or a 5xx after every retry), in any check. Nothing is known about such a page, so even a whole outage leaves a correct repair unverified, never rejected. A page the site says is gone (a 404) is different: a listing page along the next-page link that is missing fails the listing check;
- none of the sample's pages could be loaded;
- the last good run's records aren't stored (a database from before they were kept), or fewer than 20 can be compared;
- there are no anchors, or too few anchor pages load;
- the failed run's listing page isn't in the snapshots (`promote` pointed at the wrong `--snapshots`);
- a quicker check failed first (the shadow run is skipped).

A shadow run that stops at a page it can't fetch still fails the repair if the records it did compare already break the rules, whatever the rest would read: then `--force` can't help.

`heal` keeps an unverified repair, quarantines the source and says why. It doesn't ask the provider for another attempt: a new patch can't make a page load.

```
heal: quarantined: proposal 1 could not be verified: shadow run: the last good run's records are not stored; review it, then promote it with --force, or run heal again
```

`specmend promote` verifies it again first. A page that didn't load before is fetched again, so promoting later may pass without anything else. If a check still can't run, only `--force` promotes it, and never past a check that failed. **Automatic promotion never uses `--force`.**

### Blocked

A site that refuses us is not a page that failed. A 401 or 403, or a page robots.txt disallows, stops `heal` (and `promote`) as **blocked**, wherever it happens: in a quick check or in the shadow run. Nothing is promoted, the source is quarantined, a `heal_blocked` alert goes out, and the command exits with 2. There is no `--force` past it: specmend never works around a block.

```
heal: blocked: the site refused us (HTTP 403: https://example.org/notices?page=2); healing stopped and nothing was promoted. specmend never works around a block: find out why before you heal again
```

A heal reads about as many pages as a run, so a site that blocks after a number of requests may block the heal when it never blocked a run.

```
$ specmend promote examples/sources/demo.yaml 1
proposal 1 could not be verified again: shadow run: the last good run's records are not stored; pass --force to promote it anyway (never past a check that failed)
$ specmend promote examples/sources/demo.yaml 1 --force
promote: verified again: not run
  ...
  not run  shadow run: the last good run's records are not stored
  fields, on the 10 anchor pages it compared:
    reference: 10 compared, 0 changed, 0 emptied (stable: all 10 must match): none
    ...
tenders-demo: proposal 1 verified again; promoted with --force although these checks did not run: shadow run (the last good run's records are not stored); v2 is now active
```

`promote` prints the whole report, checks and fields, before it promotes anything, and with `--force` too. A shadow run that stopped at a page it couldn't fetch shows what it had compared by then: with a detail page that kept failing, a `value` that drops the millions had changed on 6 of the 29 records compared, which the rules can't fail on so few, but a human can:

```
  fields, on the 29 records of the last good run it compared before it stopped, of the 200 it would have:
    ...
    value: 29 compared, 6 changed, 0 emptied (volatile: up to 20 of 200 may change, none may become empty): tolerated 6
  records: found 29 of the last good run's 200 before it stopped: not judged
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

- it found at least 90% (`heal.shadow_min_found`) of the records the last good run had, and of each kind of record (the records of each value of a field with a small set of values, and those where a field was empty), unless chance explains what one kind is missing;
- on every record both runs have, every **stable** field reads exactly the last good run's value;
- on those records, a **volatile** field reads another value on at most 10% of them, and no value it had became empty;
- a number field changed on its 10 largest and its 10 smallest values no more often than chance allows;
- required fields are filled on records the last good run didn't have, and fields with a small set of values mostly read values from that set;
- the quick checks passed: listing, anchors, sample, known values, and no regex timed out.

With a sample (a larger source), the same holds with two differences. Coverage is judged on the items of every listing page. Values are compared on the records drawn, which always include the extremes of each number field and a record of every value of a small set.

The same checks run whether the repair is promoted by `specmend promote` or automatically, on the same pages.

## What it can't catch

- **Records the last good run didn't have** (review attack B2). Only their required fields and small sets of values are checked: there is nothing to compare them with. A plausible but wrong value on a new record passes. B2 is a `deadline` that hard-codes the year every example showed (2026), with a looser fallback that reads the first date in 2026 on the page. It reads every deadline the last good run knew right, and gives each of the 20 notices published since, whose deadlines are in 2027, its publication date. That is filled, so the required field passes. The report says how many records got only these checks (`new: 20 records the last good run didn't have: ...`), and a [provisional version](running.md#provisional-versions) keeps such an error from becoming what the next repair is compared with. Neither keeps it out of your records.
- **Records the shadow run didn't find.** Up to 10% of the last good run's records (1 − `heal.shadow_min_found`) may be missing (removed from the site, or missed by the repair), and those aren't compared, as long as no kind of record is missing more than chance explains. Missing records that are of no kind the gate knows (the last page of the listing, the oldest notices) can make up the whole 10%.
- **Kinds of record in a sample of a spec keyed by a field.** A sample judges each kind's coverage by the links on the listing pages, which are the records' keys only with `key: url`. With another key, only the share of all items found is judged; `shadow_max_items: all` judges every kind.
- **Volatile fields wrong on a few records.** Up to 10% may read another value, because prices and statuses do change. A volatile field that is wrong on fewer than 10% of records, without becoming empty, passes, unless those are a number field's largest or smallest values. The report lists how many values changed on each field, and what that tolerated.
- **A sample, for a source with more records than `heal.shadow_max_items` (default 500).** Every listing page is read, so the item container and the pagination are judged on the whole source, but only the records drawn are compared. A stable field wrong on a share *p* of records is caught with probability 1 − (1 − *p*)ⁿ for *n* records drawn. With the default 500, a field wrong on 1% of records is caught 99.3% of the time, on 0.5% about 92%, and on 0.1% only about 39%. A volatile field's changed share is estimated from the sample. Review attack B3 is a `location` that is the title's last word, wrong only on the 3 Fenwick Vale notices of 1200, none of which a random sample drew. A sample now always reads a record of every value of a field with a small set of values, so that one is caught. A field wrong on a few records that share no such value (a title in a rarer format, a date written differently on old notices) is still caught only with the probability above. **The mitigation is a full crawl: `heal.shadow_max_items: all`** reads every detail page on every heal, and compares every record. That is about one request per record: 1200 records at 1 request a second is 20 minutes a heal.
- **Changes after the verification.** `promote` verifies again on the pages `heal` stored, not on the site as it is now. A redesign in between isn't seen until the next run.
- **Pagination past `max_pages`**, and **custom strategies sharing an element**: the shared-element check needs to know which element a strategy read, which a custom strategy only says if it has a `locate(doc)` method.

**After promotion**, the runs that follow are compared with the last good run too: a field whose empty rate moves by more than 5 points, a common value of a small set (5% of records or more) that disappears, or more than 10% of a number field's values changing magnitude make the run `drift`, which alerts. A number field wrong on fewer values than that doesn't drift. A drift run doesn't become the last good run, so every run after it drifts and alerts too, until a human accepts one with `specmend accept-drift` (see [drift](running.md#drift)). Neither do the runs of a version promoted automatically, until a human confirms it. Watch the first runs after a promotion anyway.

**It can reject a correct repair** when the data really changed: a stable field corrected since the last good run on any record, more than 10% of a volatile field's values changed, more than 10% of the records gone, or a small set of values replaced wholesale (statuses renamed). `--force` can't help there: the check failed. Look at the report, then mark the field `volatile`, or loosen `heal.threshold`, `heal.volatile_change` or `heal.shadow_min_found`, in a new version of the spec file, and run it again. If you have checked the repair yourself, you can also write it into that new version: a spec file you edit is yours, not a repair, and isn't verified.

## Then what?

An accepted repair is stored as a proposal. Under `heal.mode: auto`, it is promoted to a new spec version only when all three of these hold:

- the failure carried **structural evidence**: the page fingerprints changed, `layout_changed`;
- the repair passed every check;
- the repair's confidence is at least 0.5.

Before it is promoted, it is verified again exactly as `specmend promote` would, and it is not promoted if that doesn't pass. The version it becomes is **provisional** until a human confirms it (see [provisional versions](running.md#provisional-versions)): its runs never become the last good run by themselves, so a repair that is wrong in a way the gate can't catch never becomes what the next repair is compared with. A `partial` failure (one field broken on an unchanged page) is never promoted automatically: the site may have legitimately stopped showing that field. `propose` is the default; [choosing `auto`](writing-specs.md#healing-settings) says what auto mode trades away. In `propose` mode, every accepted repair waits for:

```sh
specmend diff SPEC ID      # review it
specmend promote SPEC ID   # accept it
```

`promote` runs the gate again before anything changes, against the current anchors and the failed run's stored pages (pass the same `--snapshots` as the run; they are kept while the proposal can be promoted). The anchors may have been refreshed since, or the proposal accepted by an older version of the gate; one that no longer passes is marked rejected and not promoted (exit code 3). A proposal made for another spec version than the active one is refused; `--force` applies it to the active version instead, and verifies that.

If no repair passes, the source is quarantined, and a `repair_rejected` or `quarantined` event goes to the notifiers you configured with `--notify`. The rejected and unverified proposals stay stored, so a human can see what was tried and why it failed.

`blocked`, `transient` and `drift` are never repaired: the site refused or failed us, or real-world data changed. That isn't something a selector should fix.
