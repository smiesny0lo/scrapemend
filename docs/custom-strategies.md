# Custom strategies

The built-in strategies (css, xpath, label, regex) cover most HTML. For anything else, a plugin can add a strategy without forking scrapemend. Examples are values hidden in JSON-LD, a PDF linked from the page, or a site-specific encoding.

## A strategy is one method

```python
from scrapemend.extract import Document


class MyStrategy:
    def __init__(self, options: dict) -> None:  # the spec's `options`, as written
        self.key = options["key"]

    def extract(self, doc: Document) -> str | None:
        """The value as text, or None if not found (then the next strategy is tried)."""
        ...
```

`doc` gives you the page:

- `doc.root`: the lxml element of the page, or of the item container when fields are read from items;
- `doc.url`: the page's URL;
- `doc.text`: its visible text, whitespace collapsed.

Return text, not typed values. scrapemend applies the field's normalizers and type conversion, exactly as for built-in strategies. A strategy whose text doesn't convert loses to the next one.

## Using it in a spec

```yaml
price:
  type: decimal
  strategies:
    - custom: { name: jsonld, options: { path: offers.price, type: Product } }
    - css: "span.price"          # a fallback still works the usual way
```

`custom: jsonld` is shorthand for `custom: {name: jsonld}`.

## Registering it

Use an entry point in your plugin package's `pyproject.toml`. The factory receives the `options` and returns a strategy; a class works:

```toml
[project.entry-points."scrapemend.strategies"]
jsonld = "my_package.strategies:JsonLdStrategy"
```

Or register it in code, before the spec runs:

```python
from scrapemend.extract.strategies import register_strategy

register_strategy("jsonld", JsonLdStrategy)
```

`scrapemend validate` and every run check that each custom strategy a spec names is installed, and stop before any request if one isn't:

```
no installed plugin provides the custom strategy 'jsonld'
```

## A complete example

`examples/plugins/jsonld.py` reads schema.org JSON-LD:

```html
<script type="application/ld+json">
  {"@type": "Product", "name": "Kettle", "offers": {"price": "24.90"}}
</script>
```

It picks a value by its path (`offers.price`), optionally only from objects of one `@type`. It also skips broken JSON blocks rather than failing. `tests/unit/test_plugins.py` shows it running inside a spec, with a CSS fallback.

## Keep in mind

- Repair providers can't propose custom strategies: the anthropic provider's answer schema only includes the built-in ones. Your custom strategies stay as you wrote them, and a repair adds built-in strategies around them.
- Strategies must not fetch anything themselves. All requests go through the fetcher, which applies robots.txt and rate limits. If you need another document (a linked PDF, an API response), that's a fetcher or pipeline extension, not a strategy.
