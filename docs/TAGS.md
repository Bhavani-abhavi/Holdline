# Phase tags

Created locally during the build. The build environment's git proxy refused `refs/tags` pushes
(commits pushed fine), so create them on GitHub from a normal checkout:

```
git tag -a v0.1-foundation <sha> -m "Phase 1: foundation"
...
git push origin --tags
```

| Tag | Phase | Commit |
|---|---|---|
| `v0.1-foundation` | Phase 1: foundation | da64ba7 |
| `v0.2-domain` | Phase 2: domain core, concurrency proven | da64ba7 |
| `v0.3-agent` | Phase 3: grounded agent with tiered guardrails | 014d91c |
| `v0.4-interfaces` | Phase 4: interfaces | 53f62ae |
| `v0.7-evals` | Phase 7: evaluation harness | 53f62ae |
