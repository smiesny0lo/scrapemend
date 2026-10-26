"""An example plugin: a strategy that reads JSON-LD structured data.

Many sites embed schema.org data in their pages:

    <script type="application/ld+json">
      {"@type": "Product", "name": "Kettle", "offers": {"price": "24.90"}}
    </script>

In a spec, pick a value by its path, optionally only from objects of one @type:

    price:
      type: decimal
      strategies:
        - custom: { name: jsonld, options: { path: offers.price, type: Product } }
        - css: "span.price"

A plugin package registers the strategy in its pyproject.toml, so specmend finds
it without any import:

    [project.entry-points."specmend.strategies"]
    jsonld = "examples.plugins.jsonld:JsonLdStrategy"

or, in Python, with specmend.extract.strategies.register_strategy("jsonld", ...).
"""

import json
from typing import Any

from specmend.extract import Document


class JsonLdStrategy:
    """Built from the spec's `options`; finds the value in the page's JSON-LD."""

    def __init__(self, options: dict[str, Any]) -> None:
        if "path" not in options:
            raise ValueError("the jsonld strategy needs options.path, e.g. offers.price")
        self.path = str(options["path"]).split(".")
        self.type = options.get("type")

    def extract(self, doc: Document) -> str | None:
        for script in doc.root.xpath("//script[@type='application/ld+json']"):
            try:
                data = json.loads(script.text or "")
            except json.JSONDecodeError:
                continue  # one broken block shouldn't hide the others
            for item in data if isinstance(data, list) else [data]:
                if not isinstance(item, dict) or (self.type and item.get("@type") != self.type):
                    continue
                value: Any = item
                for key in self.path:
                    value = value.get(key) if isinstance(value, dict) else None
                if value is not None and not isinstance(value, dict | list):
                    return str(value).strip() or None
        return None
