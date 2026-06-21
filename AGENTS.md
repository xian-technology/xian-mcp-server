# Repository Guidelines

## Local Knowledge Graph
- If `graphify-out/graph.json` exists, prefer `graphify query`, `graphify path`, or `graphify explain` for broad architecture and impact questions before scanning files manually.
- Treat `graphify-out/` as a generated local artifact; it is intentionally ignored by Git.
- After structural code changes, refresh the local graph with `graphify update .` when useful.
