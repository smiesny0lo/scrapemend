# Repair fixtures

Hand-written repairs of `examples/sources/demo.yaml` that the verification gate must
**reject**. The correct repairs live in `examples/repairs/`.

- `v2-wrong-field.yaml`: every selector works, but `location` reads the category.
- `v3-swapped-dates.yaml`: `deadline` reads the publication date.
- `v2-empty.yaml`: selectors that match nothing on the new layout.

Overfitted repairs (strategies that only match the anchors a provider was shown)
are built in the tests from the shown anchors' values, since which anchors are
shown depends on the URLs of the test server.
