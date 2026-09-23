# View and ChunkGroup

Two chunks in the `Core` category that don't do anything to the stage themselves — they
shape *when* other chunks cook and *how the graph is organized*. Both ship in
[`scripts/core/`](/scripts/core/): [`view.py`](/scripts/core/view.py) and
[`chunkgroup.py`](/scripts/core/chunkgroup.py).

## View

A network with no View chunk cooks nothing at all. Execution is pull-based: nothing runs
speculatively, only what a view's upstream tree actually needs.

Drop a View chunk, wire the end of an exec chain into it, and it becomes that chain's
**active view** — the thing that decides what needs to cook. Everything upstream of the
active view (its whole dependency tree, not just its immediate input) cooks; everything
else in the same context is blocked and muted, whatever else is wired up. That's what makes
a network safe to leave half-built: an unfinished branch sitting off to the side simply
never runs until something makes it, or a view of its own, the active one.

- **Place with `V`.** The graph editor's `V` shortcut drops a View chunk at the cursor —
  the normal way to add one, rather than finding it in the create menu.
- **Terminal.** View sets `ChunkConfig.terminal=True` — it has no `outputs:chunk`, so it
  can only end a chain, never continue one.
- **Per context, not global.** Each graph context — the top-level graph, a
  [ChunkGroup](#chunkgroup)'s inner graph — has its own view slots (up to three, numbered
  1–3), switchable without disturbing any other context's. A tab and a group dive can each
  independently have their own slot 1 active at the same time. Views not currently active
  are dimmed; a result that's gone stale because its upstream changed since it last cooked
  is highlighted amber rather than silently looking current.
- **Open a saved file and it opens on whatever view was active when it was saved** — that
  state is recorded on the context's own prim (`chunk:activeView`), so it round-trips the
  same in every frontend that reads it.

A [ChunkGroup](#chunkgroup)'s *inner* view is also what drives its single collapsed output
on the outer graph — see below.

## ChunkGroup

An empty container chunk — a subnet. Drop one, double-click it to dive inside, and build a
network there exactly as you would at the top level; ChunkGroup only ever holds the `doc`
param described below, everything else inside it is chunks of your own.

- **Nesting is real USD nesting.** A group's contents live under its own prim path —
  `/chunks/ChunkGroup/chunks/...` — not in some side table, so the top-level graph stays
  readable and a group is just another subtree of the stage.
- **One output, driven by its inner view.** A group presents to the outer graph as a single
  node; what that node produces is whatever the group's own active [View](#view) currently
  resolves to inside it. Change which inner view is active and the group's outward-facing
  result changes with it.
- **Params cross the boundary by lofting, not by reference.** A param you want visible on
  the group node itself (so the outer graph can drive it, rather than diving in every time)
  is *lofted* — the outer param is a real param on the group chunk, and the connection
  points **up**, from the inner param to the group, not sideways between two inner chunks.
  Don't build or read a loft as if it were an ordinary `@Node.param@` expression between
  siblings; it isn't one.
- **Groups are files.** Save a group as its own `.usda` and load it back into another scene
  by sublayer, reference or payload — the same three composition arcs USD always offers.
  Chunks that arrive this way, from a layer you don't own, show up as **locked** nodes: an
  amber header, and auto-layout treats them as fixed obstacles rather than moving them.
- **`doc` describes the group's interface.** ChunkGroup's one param is a multi-line text
  field for describing what the network inside does and how to drive it — written for
  whoever (or whatever) opens the saved group without diving in first, human or LLM.

### The `CONTEXT` group flag

Both View's container and ChunkGroup rely on `ChunkFlags.GroupFlags.CONTEXT` under the
hood: a context group is drawn as a *workspace holding other chunks*, not as a node with
ports of its own. In the Qt frontend that's a tab; other frontends may render it
differently, but the meaning — "this is a graph, not a chunk" — is the same everywhere. See
[Writing a graph frontend](graph-frontends.md) if you're implementing that rendering for a
new host.
