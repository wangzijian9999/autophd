# autophd Research Protocol

## Hard Rules

1. Every request must start with explicit reasoning and source verification when external facts are involved.
2. Every candidate run starts from the current verified parent.
3. A failed, crashed, degraded, or insufficiently supported candidate is diagnostic only.
4. A candidate may become a new parent only after the configured metric gates, non-regression gates, mechanism-evidence gates, and review gates pass.
5. No fabricated data, citations, SOTA claims, experimental results, or method effects are allowed.
6. If evidence is insufficient, the result must explicitly say `insufficient information`, `uncertain`, or `unknown`.

## Default Loop

```text
research brief
-> idea adapter
-> Codex patch
-> allowed-path check
-> sanity checks
-> training command
-> evaluation command
-> metric extraction
-> Codex reflection
-> Claude Code review
-> gate decision
-> promote commit or rollback
```

## Parent Promotion

Promotion requires:

- Primary metric improvement against the configured baseline or latest parent.
- No configured non-regression metric violation.
- Required mechanism-evidence files are present when enabled.
- Required external review is available when enabled.

Model agreement alone is never enough for promotion.

