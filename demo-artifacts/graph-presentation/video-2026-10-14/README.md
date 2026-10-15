# Graph generation in two minutes

Play `graph-generation-2-minutes.mp4`. It is exactly 120 seconds, 1920×1080 at
24 fps, with English narration, visible captions and ten chapter markers for
players that support them. `captions.srt` and `transcript.md` are also provided.

The phases use simple language: **Read → Find → Connect → Check → Save → Review → Publish**.
The example is **“The library app uses a book list.”** A second help note mentions
the same app, allowing the demo to show entity merging and source traceability.

## What actually ran

`run_live_example.py` calls the project's `graphs.build_graph`,
`graph_ai.enrich_graph`, `graph_ai.judge` and `graph_quality.themes` functions.
The two synthetic documents produce structural nodes, two accepted meaning links,
three entity nodes and one shared library-app entity. The invented toy-list quote
is rejected. Graphify clustering is executed on the resulting graph.

AI extraction proposals are controlled for reproducibility. No paid provider call
is made, and production source records are not loaded or modified. Saving and
publication are explained visually; this example does not write a graph revision
or publish a production graph.

## Technical points behind the plain language

| Phase | Project technology | What it contributes |
|---|---|---|
| Read | MarkItDown conversion pipeline | Converts uploaded documents into usable text. The worked example starts with already-normalized Markdown. |
| Find | `graphs.py`, Python | Finds document, section and table structure. |
| Connect | Optional OpenAI/Claude provider; `graph_ai.py` | Proposes typed relationships with exact source quotations. |
| Check | `graph_ai.judge`, `ontology.py`, SHA-256 | Checks quoted words, named entities and allowed types; keeps source fingerprints. |
| Save | Django graph JSON and numbered revisions | Stores a draft snapshot with provenance. |
| Review | Quality checks; Graphify, NetworkX and Leiden | Shows coverage and groups existing connected nodes. Clustering does not establish factual correctness. |
| Publish | Human review and published-revision workflow | Makes a reviewed version available to graph answers; access and current evidence are checked. |

The visuals use source wording. Internally, the ontology normalizes **uses** to
**depends on**, and **explains** to **covers**. This preserves the project's actual
saved schema while keeping the explanation easy to follow. Quote/type validation
filters errors; it does not prove the truth of every interpretation.

## Files and verification

- `live-example.json`: full actual builder result and validation outcomes.
- `process-diagram.html`: interactive companion architecture diagram.
- `verification.json`: media properties, decode check, hashes and review scope.
- `narration.wav`: separate editable audio, generated locally with Microsoft Zira.
- `storyboard.json`: chapter timing and narration text.
- `build_video.py`, `narrate.ps1`, `verify_video.py`: reproducible build sources.

The companion diagram passed 9/9 showcase checks with no errors or warnings,
browser containment checks and visual review. Frames sampled from every video
chapter were inspected, and the complete media file decoded without errors.

To rebuild, run the live example with the project's Python environment, generate
the narration on Windows, then run `build_video.py` and `verify_video.py`. FFmpeg,
FFprobe, Pillow and the Segoe UI fonts are required. The companion diagram is
generated separately through Archify; its receipts remain bound to its exact bytes.
