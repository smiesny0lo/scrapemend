# Writing specs

A **source spec** is a YAML file that tells scrapemend how to scrape one website. It is configuration, not code: when the site changes, a repair edits the spec, never Python.

`examples/sources/demo.yaml` is a complete, commented spec for the demo site. Check any spec with:

```sh
scrapemend validate my-source.yaml
```

Every problem is reported with its line and column, for example `line 14, col 9: fields.title.stratgies: unknown key`.

## The shape of a spec

```yaml
id: tenders-demo          # lowercase letters, digits, - and _
version: 1                # bump it whenever you edit the spec by hand

fetch:
  start: https://example.org/notices
  paginate:
    next: { css: "li.next a::attr(href)" }
    max_pages: 50
  rate: { rps: 1 }        # requests per second, per domain
  timeout: 30             # seconds per request

item:
  container: { css: "table.notices tr.notice" }   # one match per item on a listing page
  follow: { css: "td.title a::attr(href)" }       # the item's detail page (optional)
  key: url                # what identifies an item across runs

fields:
  title:
    required: true
    strategies:
      - css: "h1.notice-title"
      - label: Title

expect:
  min_items_per_run: 100
  max_null_rate: { value: 0.15 }

heal:
  mode: propose           # off | propose | auto
  anchors: 10
  threshold: 0.9
```

How a run works:

1. It fetches `fetch.start`.
2. Every element matching `item.container` is an item.
3. With `item.follow`, each item's detail page is fetched and the fields are read there. Without it, they are read from the item itself.
4. Then the run follows `fetch.paginate.next`, for up to `max_pages` pages.

### Keys

`item.key` names the field that identifies an item across runs. Anchors (see [verification](verification.md)) depend on it.

- `url`, the default, uses the detail page's URL and needs `item.follow`. URLs usually survive redesigns, which makes them a good key.
- A field name also works, but that field must be `required: true`. `url` itself is reserved and can't be a field name.

## Fields

```yaml
deadline:
  type: date               # str (default), int, decimal, date, url
  required: true           # a record without it is invalid
  volatile: false          # true: the value may legitimately change (prices, statuses)
  date_format: "%d %b %Y"  # strptime format; type: date only
  normalize: [strip]       # strip, collapse_whitespace, lower, strip_currency
  null_values: ["N/A"]     # texts that mean "no value" (any case)
  pattern: 'TN-\d{4}-\d{4}' # the whole value must match; str and url only
  strategies: [...]
```

How a field's text becomes a value:

- **Strategies** are tried in order. The first one whose text **converts to the field's type** wins: a CSS match reading "See documents" is not a price, so the next strategy gets its chance. Each record keeps which strategy won. A fallback winning is an early warning that the page is changing.
- **Normalizers** run before conversion. `strip_currency` removes symbols (€, $, £ …), codes (EUR, CZK …) and a trailing `,-`.
- **Numbers.**
  - `decimal` accepts both decimal marks: `1,234.56`, `1.234,56`, `1 234,50` and `1'234.50`.
  - A lone `,` followed by exactly three digits is a thousands separator (`1,234`). Otherwise it's a decimal comma (`12,5`).
  - A lone `.` is always a decimal point.
  - If a site writes thousands as `1.234`, add a strategy or normalizer that makes that unambiguous.
- **Dates.** `date` tries ISO and a few common formats (`3 Nov 2026`, `03.11.2026`, `3. 11. 2026` …). It never guesses between `03/11` and `11/03`: set `date_format` for those.
- **`url`** values are resolved against the page they came from.
- **`null_values`** and empty fields:
  - A text listed in `null_values` gives `null`, and the field still counts as found.
  - Text that is found but doesn't convert gives `null` and is reported as *unusable*.

## Strategies

| Strategy | Finds | Example |
|---|---|---|
| `css` | the first matching element's text, or an attribute with `::attr(name)` | `css: "a.next::attr(href)"` |
| `xpath` | the first result: an element's text, an `@attribute`, or a `string(...)`/`count(...)` value | `xpath: "//dt[.='Deadline']/following-sibling::dd[1]"` |
| `label` | the value next to a visible label | `label: Deadline` |
| `regex` | the first match in the visible text; capture group 1 is the value | `regex: 'Ref\.\s*(TN-\d+)'` |
| `custom` | a strategy from a plugin, see [custom strategies](custom-strategies.md) | `custom: { name: jsonld, options: {...} }` |

Details:

- **Text** is the visible text, with whitespace collapsed. Separate block elements are separated by a space (`<dt>A</dt><dd>B</dd>` reads `A B`). Inline elements are not (`<b>Hel</b>lo` reads `Hello`). Scripts and styles are never part of the text.
- **CSS** goes through cssselect, so `:nth-child`, `:not()` and `:has()` work.
- **`label`** has two relations:
  - `next_sibling` (the default): an element whose whole text is the label. The value is its next sibling element, as in `<dt>Deadline</dt><dd>3 Nov</dd>`.
  - `{text: Deadline, relation: after}`: the rest of a *single line of text* that starts with the label, as in `<p>Deadline: 3 Nov</p>` or `<p><b>Deadline</b> 3 Nov</p>`.

  Labels match case-insensitively, ignoring extra spaces and a trailing colon.
- **`regex`** can search one element instead of the whole page. That's much safer when a line holds several values:

  ```yaml
  - regex: ' · (.+)$'
    within: { css: "div.meta" }   # "Construction · Dunmere" -> "Dunmere"
  ```

- **XPath inside items.** When fields are read from the item container (no `follow`), start XPath with `.//` to stay inside the item. `//` always searches the whole page.

## Expectations

`expect` describes what a healthy run looks like:

- **`min_items_per_run`** only applies to complete runs, not ones cut short with `--limit` or `check`.
- **`max_null_rate`** gives the share of records in which a field may be empty. Rates are judged on complete runs, or from 20 records up.

## Healing settings

`heal.mode` decides what happens when a run fails with a layout problem:

- **`off`:** report only.
- **`propose`** (the default): a repair that passes verification waits for `scrapemend promote`.
- **`auto`:** a repair is promoted at once, provided three things hold:
  - it passed verification;
  - the failure carries structural evidence (the page fingerprint changed);
  - the repair's confidence is at least 0.5.

The other settings:

- **`heal.anchors`** is the number of known-good records kept for verifying repairs.
- **`heal.threshold`** is the share of anchor values *every* field must reproduce for a repair to pass (default 0.9).

## Versions

Specs are versioned and never edited in place. The first run of a spec file registers its `version`. A promoted repair becomes the next version, and `scrapemend rollback` makes an older version active again.

If you edit a spec file, **bump its `version`**. A changed file reusing a stored version number is refused, so a version number always means one exact spec. A newer file version becomes active. An older one doesn't undo a promoted repair.

```sh
scrapemend versions my-source.yaml      # * marks the active version
scrapemend show my-source.yaml --version 2
scrapemend diff my-source.yaml --from 1 --to 2
```
