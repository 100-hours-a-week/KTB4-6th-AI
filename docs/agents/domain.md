# Domain Docs

Read `CONTEXT.md` and applicable files in `docs/adr/` before exploring a domain area. If they do not exist, proceed silently: `/domain-modeling` creates `CONTEXT.md` when the glossary is needed, and `/adr` creates the decision log when the first durable decision is made.

## File structure

This is a single-context repository:

```
/
├── CONTEXT.md
├── docs/adr/
└── src/
```

Use vocabulary defined in `CONTEXT.md`. If a proposed change conflicts with an ADR, surface the conflict explicitly.
