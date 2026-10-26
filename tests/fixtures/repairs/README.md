# Repair fixtures

Hand-written repairs of `examples/sources/demo.yaml` that the verification gate must
**reject**. The correct repairs live in `examples/repairs/`.

- `v2-wrong-field.yaml`: every selector works, but `location` reads the category.
- `v3-swapped-dates.yaml`: `deadline` reads the publication date.
- `v2-empty.yaml`: selectors that match nothing on the new layout.
- `v2-status-reads-category.yaml`, `v3-status-reads-category.yaml`: `status`, a
  volatile field, reads the category.
- `v2-first-pager-link.yaml`, `v3-page-2.yaml`: next-page links that loop or stop.
- `v2-header-row.yaml`: an item container that also matches the table's header row.
- `listing-v2-title-reads-authority.yaml`: a wrong field in a listing-only spec.

From the publish review of v0.1, the correct repair with one defect each. Most are
right on the records a provider is shown and wrong on a few others, which the anchors
used to miss:

| Fixture | Review | Defect |
|---|---|---|
| `v2-published-reads-deadline.yaml` | A1 | `published` reads the deadline's row |
| `v3-description-wrong-element.yaml` | A2 | `description` reads the deadline box's paragraph |
| `v2-value-fitted-to-one-page.yaml` | A3 | `value` only reads five-digit amounts: 102 of 200 wrong |
| `v2-value-drops-millions.yaml` | S1 | `value` drops the millions: 29 of 200 wrong |
| `v2-status-enumerates-examples.yaml` | S2 | `status` only knows Open and Closed: 39 of 200 empty |
| `v2-location-last-word.yaml` | S3b | `location` is the title's last word: 12 of 200 wrong |
| `v3-location-last-word.yaml` | S3 | the same, on layout v3 |
| `v2-redos.yaml` | R | a `description` regex that backtracks catastrophically |

From the second review, of the shadow-run gate: wrong in ways the shadow run used to let through.

| Fixture | Review | Defect |
|---|---|---|
| `v2-next-stops-at-page-10.yaml` | B6 | the next-page link stops at page 10: on 1200 notices, 1020 never read again |

Overfitted repairs (strategies that only match the anchors a provider was shown)
are built in the tests from the shown anchors' values, since which anchors are
shown depends on the anchors the good run kept.
