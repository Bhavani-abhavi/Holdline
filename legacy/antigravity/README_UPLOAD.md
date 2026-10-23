# Antigravity build (partial upload)

These files are the earlier HoldLine build made in Antigravity, uploaded manually on
2026-10-23 (commit 09b841b, preserved unchanged on branch `antigravity-upload`).

What arrived: the root documents (README, DEFECTS, pyproject, .env.example, .gitignore,
.gitattributes) and the contents of its `tests/` folder.

What did NOT arrive and is still needed to run these tests: its `src/holdline/` package
(`agent_manual.py`, `agent_graph.py`, `timeutils.py`, `seed_data.py`, `voice.py`, `spoken.py`,
and the rest), its `docs/` and `evals/` folders. Upload those folders into this directory.

Nothing here is on the import path of the rebuild; `pytest` at the repository root runs only
`tests/`.
