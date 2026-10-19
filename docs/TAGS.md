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
