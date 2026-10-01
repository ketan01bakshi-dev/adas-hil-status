# Agent instructions

This repository’s Cursor behavior is defined by project rules in `.cursor/rules/`. They are **always applied**. Do not wait for an `@`-mention.

## Non-negotiable: source grounding

Follow `.cursor/rules/00-source-grounding.mdc` on every task. Facts, APIs, copy, diagrams, and metrics come only from workspace files, the user, and tool results. Cite sources. If it is not in sources, say so.

## Specialized rules (always on; apply by domain)

| Domain | Rule file |
| --- | --- |
| React, Tailwind, web components | `.cursor/rules/webui.mdc` |
| Kotlin, Jetpack Compose, MVVM | `.cursor/rules/android.mdc` |
| API refs, schemas, Mermaid, mind maps | `.cursor/rules/tech-docs.mdc` |
| Messaging, feature guides, copy | `.cursor/rules/marketing-docs.mdc` |

When work spans domains, apply every matching rule. Source grounding overrides completeness.
