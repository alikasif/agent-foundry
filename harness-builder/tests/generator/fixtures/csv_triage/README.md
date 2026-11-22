# csv-bug-triage

Triages bug reports exported as CSV (columns `id`, `title`, `severity`,
`status`).

## Tools

- `triage_by_severity(csv_text)`: bug ids grouped by severity.
- `count_by_status(csv_text)`: number of bugs per status.

## Assumptions

- Severities are critical, high, medium and low; anything else is `unknown`.
- The CSV has a header row.

## Usage

```
harness run <this directory> "Triage: <paste the CSV>"
```
