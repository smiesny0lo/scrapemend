"""Synthetic tender notices, generated deterministically from a fixed seed.

Every value is invented. The same seed always yields the same records, so every
layout renders identical content and tests can assert exact values.
"""

import random
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

SEED = 2026
COUNT = 200

SUBJECTS: dict[str, tuple[str, ...]] = {
    "Construction": (
        "Road resurfacing works",
        "School roof renovation",
        "Bridge maintenance",
        "Sports hall extension",
    ),
    "IT services": (
        "Website redevelopment",
        "Network hardware supply",
        "Helpdesk support services",
        "Document management system",
    ),
    "Cleaning": ("Office cleaning services", "Window cleaning", "Street sweeping"),
    "Consulting": (
        "Urban mobility study",
        "Energy audit of public buildings",
        "Legal advisory services",
    ),
    "Medical supplies": (
        "Disposable gloves supply",
        "Patient monitoring equipment",
        "Wound care products",
    ),
    "Transport": ("School bus services", "Fleet vehicle leasing", "Winter road gritting"),
    "Energy": ("Electricity supply", "Solar panel installation", "Street lighting upgrade"),
}
PLACES = (
    "Ashgrove",
    "Brindlemoor",
    "Cobbleford",
    "Dunmere",
    "Fenwick Vale",
    "Glenhollow",
    "Harrowfield",
    "Kestrelby",
    "Larkmoor",
    "Millbeck",
    "Northwold",
    "Oakhaven",
    "Pebblestone",
    "Ravensholm",
    "Stillwater",
    "Thistledown",
)
AUTHORITIES = (
    "City of {place}",
    "Municipality of {place}",
    "{place} County Council",
    "{place} Regional Hospital",
    "{place} Water Authority",
    "{place} School District",
)
SCOPES = (
    "design, delivery and two years of maintenance",
    "supply and installation",
    "ongoing service provision",
    "fixed-price delivery with optional extensions",
)
STATUSES = ("Open", "Closed", "Awarded")
STATUS_WEIGHTS = (5, 3, 2)
UNDISCLOSED_VALUE_RATE = 0.08  # some notices hide their value, as real ones do
FIRST_PUBLISHED = date(2026, 1, 5)
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
CENTS = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class Notice:
    id: str
    title: str
    authority: str
    category: str
    location: str
    published: date
    deadline: date
    value: Decimal | None
    status: str
    description: str


def generate_notices(seed: int = SEED, count: int = COUNT) -> list[Notice]:
    rng = random.Random(seed)
    notices = []
    for i in range(1, count + 1):
        category = rng.choice(list(SUBJECTS))
        subject = rng.choice(SUBJECTS[category])
        place = rng.choice(PLACES)
        authority = rng.choice(AUTHORITIES).format(place=place)
        published = FIRST_PUBLISHED + timedelta(days=rng.randrange(270))
        deadline = published + timedelta(days=rng.randint(14, 60))
        value = (
            None
            if rng.random() < UNDISCLOSED_VALUE_RATE
            else Decimal(round(10 ** rng.uniform(4, 6.4), -2)).quantize(CENTS)
        )
        months = rng.choice((6, 12, 24, 36, 48))
        notices.append(
            Notice(
                id=f"TN-2026-{i:04d}",
                title=f"{subject} - {place}",
                authority=authority,
                category=category,
                location=place,
                published=published,
                deadline=deadline,
                value=value,
                status=rng.choices(STATUSES, weights=STATUS_WEIGHTS)[0],
                description=(
                    f"{authority} invites tenders for {subject.lower()} in {place}. "
                    f"The contract covers {rng.choice(SCOPES)} over {months} months."
                ),
            )
        )
    return notices


# Display formats are part of the content: every layout uses these, so values
# render identically no matter which markup surrounds them.
def format_date(d: date) -> str:
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def format_money(value: Decimal | None) -> str:
    return "Not disclosed" if value is None else f"€{value:,.2f}"
