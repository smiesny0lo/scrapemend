# Repair providers

A repair provider looks at a broken spec and proposes a fix. Its answer is **data, never code**: a `SpecPatch` that the [verification gate](verification.md) then judges on pages under the new layout. Nothing a provider returns is executed.

## Built-in providers

| Provider | Use |
|---|---|
| `none` (default) | proposes nothing. Failures are detected and reported, and a human repairs the spec. scrapemend works fully offline. |
| `--patch FILE` | a repair you wrote by hand, as YAML. It goes through the same gate as any other. |
| `anthropic` | Claude proposes the repair. Install with `pip install "scrapemend[anthropic]"` and set `ANTHROPIC_API_KEY`. |

A hand-written patch looks like `examples/repairs/demo-v2.yaml`:

```yaml
rationale: Layout v2 renamed every class.
confidence: 0.9
container: { css: "table.tender-table tr.tender-row" }
follow: { css: "a.tender-link::attr(href)" }
next_page: { css: "a[rel=next]::attr(href)" }
fields:
  title:
    strategies: [{ css: "h1.tender__title" }]
```

```sh
scrapemend heal my-source.yaml --patch my-fix.yaml
```

## What a patch can change

A `SpecPatch` can set new selectors for the item container, the follow link and the next-page link. It can also give existing fields new strategies, along with their `normalize`, `date_format` and `null_values`. A field's [custom strategies](custom-strategies.md) are kept: the patch's strategies are added around them.

It **cannot** touch:

- the start URL or the rate limit;
- field types or `required`;
- expectations or heal settings.

So a repair, however it was produced, can never point the scraper at another site, make it less polite, or loosen the checks it is verified against. Applying a patch creates a new spec version, validated like any spec file.

## The anthropic provider

The provider sends Claude a **repair context**:

- the current spec as YAML;
- what broke, and which fields are failing;
- pruned HTML of a listing page and a detail page, before (when the spec worked) and now;
- three of the anchors as examples.

**Pruning** removes scripts, styles and comments, keeps only selector-relevant attributes, shortens long texts, and cuts repeated listing items down to three. A typical context is 7–8k tokens.

Claude answers with **structured output**: JSON constrained to a schema, which the SDK validates and scrapemend turns into a `SpecPatch` and validates again. If the gate rejects an attempt, its problems are sent back for a second try (`--attempts`, default 2). The unchanged context is served from the prompt cache.

Settings:

- **`--model`** or `SCRAPEMEND_ANTHROPIC_MODEL`: the model to use, `claude-opus-5-5` by default.
- **Effort** is `high`.
- **Refusal fallbacks** are enabled through the server-side fallback beta, so a safety decline is retried on a fallback model inside the same call.

On the demo site, one repair takes 15–25 s and roughly 7.5k input and 2k output tokens, about $0.07–0.09 at Claude Opus 5.5 prices. In testing, Claude repaired both demo redesigns on the first attempt, with every field reproducing 10/10 anchor values. Those answers are recorded in `tests/fixtures/recorded/` and replayed in CI.

**Untrusted input.** Page HTML comes from the scraped website. The prompt marks it as data and tells the model to ignore instructions inside it. The patch schema and the gate are the real defence: even a manipulated answer can only propose selectors and strategies, and those must reproduce the known values on the pages the gate checks (see [what the gate can and can't catch](verification.md#what-the-gate-can-and-cant-catch)).

## Writing your own provider

A provider is any object with a `name` and a `propose` method:

```python
from scrapemend.heal.context import RepairContext
from scrapemend.spec.patch import SpecPatch


class MyProvider:
    name = "my-provider"

    def propose(self, context: RepairContext) -> SpecPatch | None:
        # context.spec_yaml, context.failures, context.failing
        # context.old_listing / new_listing / old_detail / new_detail  (pruned HTML)
        # context.examples: url, values, shown_as (the text on the old page)
        # context.attempts: earlier patches and the problems verification found
        ...
        return SpecPatch.model_validate({"fields": {"title": {"strategies": [{"css": "h1"}]}}})
```

- Return `None` when you have nothing (more) to suggest.
- Raise `scrapemend.heal.providers.ProviderError` when you can't produce a repair, for example when an API fails.
- `rationale` and `confidence` (0–1) on the patch are shown to whoever reviews it. Confidence below 0.5 also blocks automatic promotion.

Register the provider in your plugin package's `pyproject.toml`, then use `--provider my-provider`:

```toml
[project.entry-points."scrapemend.providers"]
my-provider = "my_package.providers:MyProvider"
```
