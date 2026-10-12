"""Root-cause analysis for scraper breakage. Accuracy not guaranteed."""

import random

EXCUSES: tuple[str, ...] = (
    "The frontend team discovered Tailwind again. Every class is now `flex`.",
    "Someone wrapped the price in seven nested <div>s for 'accessibility'.",
    'The site migrated to React. The HTML is now a single <div id="root">.',
    "An A/B test is live. You are in bucket B. Bucket B has no data.",
    "The intern renamed `product-title` to `productTitle` on a Friday.",
    "Cloudflare looked at your User-Agent and felt something.",
    "The CSS classes are now hashed. Your selector was `.sc-1x8k2fq`. It is gone.",
    "Marketing added a cookie banner that is 4000px tall.",
    "The table is now an image of a table.",
    "Mercury is in retrograde. This is not a joke. Check the logs.",
    "It works on the dev's machine. The dev's machine is not the internet.",
    "The site went 'headless'. So did your scraper.",
)


def random_excuse(rng: random.Random | None = None) -> str:
    """Return a plausible, unverifiable reason the scrape failed."""
    return (rng or random.Random()).choice(EXCUSES)
