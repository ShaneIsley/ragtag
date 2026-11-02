# Diagrams

The diagrams in [`../design.md`](../design.md) and the repository's [`README.md`](../../README.md), drawn in [D2](https://d2lang.com). Each `.d2` file is the source; the `.svg` beside it is what the design shows, and is committed so the forge displays it without D2.

| Diagram | Where it's shown |
|---|---|
| [`overview`](overview.svg) | `README.md`, How it works |
| [`context`](context.svg) | 3.1 Context |
| [`components-answer`](components-answer.svg), [`components-jobs`](components-jobs.svg) | 3.2 Components and where they run |
| [`answer-sequence`](answer-sequence.svg) | 4.1 Answering a question |
| [`decline-rules`](decline-rules.svg) | 4.2 Retrieval and the decline rules |
| [`ingest`](ingest.svg) | 4.5 Ingest |
| [`security`](security.svg) | 4.7 Security |
| [`dataflow`](dataflow.svg) | 4.8 Data and privacy |
| [`deployment`](deployment.svg) | 4.9 Deployment |
| [`telemetry`](telemetry.svg) | 4.10 Observability |
| [`evaluation`](evaluation.svg) | 4.11 Evaluation |
| [`drift`](drift.svg) | 4.11 Evaluation, drift checks |

## Changing one

1. Install D2: `brew install d2`. The SVGs here were made with 0.7.1.
2. Edit the `.d2` file. `_shared.d2` holds what every diagram shares: the layout engine (ELK), the light and dark themes, the icon addresses and the styles. Planned parts use the `planned` class, which draws them dashed.
3. Run `make diagrams` from the repository root. It renders every diagram, fetching the icons from icons.terrastruct.com and embedding them in each SVG.
4. Commit the `.d2` and `.svg` files together, with the design change they show.

`d2 --watch docs/diagrams/<name>.d2` opens a live preview while you edit.
