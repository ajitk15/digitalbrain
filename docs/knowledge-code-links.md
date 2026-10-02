# Knowledge-to-code references

Knowledge and code retain independent snapshots. A read-only projection connects
them without changing a published knowledge graph or re-indexing a repository.

Select a knowledge node to see **Related code** in its inspector. Each result
opens the code file and includes the source passage, knowledge revision,
repository commit, and matching code passage where applicable. Code files show
**Related knowledge**, with a return link that focuses the original graph node.
The reverse view uses the published knowledge revision; the forward view uses
the selected revision and the latest indexed snapshot of each active repository.

**Expand related code**, in the toolbar beside **Overview**, becomes available
when the selected node has verified code references. It draws up to 30 distinct code files beside the selected
knowledge node and its immediate neighbours. Blue square nodes and blue
**Code reference** edges distinguish code from knowledge. Select a code node
to inspect its source evidence and commit, open Code Graph, or return to the
knowledge node. Repeated expansion does not duplicate nodes. **Overview** removes
the temporary code overlay. This changes only the browser view, never the saved
or exported knowledge graph.

Two deterministic reference types are supported:

- An explicit source filename/path resolves uniquely within the repository.
- A source passage and a code file contain the same exact identifier, such as
  `FR-09`. The UI calls this a shared identifier, not an implementation claim.

Every source must still be active, its digest must match both its content and
the graph evidence, and the quoted passage must occur on the recorded line.
Code digests are checked too. Ambiguous path suffixes are omitted. Both feature
permissions and application membership are checked before crossing graphs.

Code Factory uses identifiers in its analysis question to select source passages,
then prioritizes files linked by those passages in its pinned code snapshot.
Existing keyword retrieval remains the fallback. Citation metadata retains the
link, and plan approval revalidates it. Existing runs are not rewritten.

The initial implementation scans up to 2,000 files / 8 MB per snapshot, ten
repositories per forward lookup, and returns up to 100 links. The UI reports
truncation. Missing links do not prove missing behavior. Arbitrary semantic
matching, manually confirmed links, and persisted cross-link indexes are future
extensions; no database migration or graph regeneration is required here.
