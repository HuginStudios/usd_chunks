# View and ChunkGroup

Two chunks that don't touch the stage themselves — they shape *when* things cook and *how
a graph is organized*. Everything else in
[`scripts/`](https://github.com/HuginStudios/usd_chunks/tree/main/scripts) does something
to your scene; these two are structural. Both ship in
[`scripts/core/`](https://github.com/HuginStudios/usd_chunks/tree/main/scripts/core):
[`view.py`](https://github.com/HuginStudios/usd_chunks/blob/main/scripts/core/view.py) and
[`chunkgroup.py`](https://github.com/HuginStudios/usd_chunks/blob/main/scripts/core/chunkgroup.py).

## 🔎 At a glance

- **View** — right-click a chunk, choose **Set View**, and its output shows up in the
  viewport. Nothing else cooks until you do this.
- **ChunkGroup** — a subnet. Double-click to dive in and build inside it like any other
  graph; double-click out when you're done.

The rest of this page is the detail behind both.

## View

Wire nothing to a View and your whole network sits there, fully built, doing nothing —
**in the UI.** That's the deal there: only what a view actually needs cooks, nothing
runs speculatively. Headless is the other way round: `usdchunks run` cooks *everything* in
a file that has no view, and `--all` forces that same everything-cooks behaviour even when
a view exists. A view is what narrows execution, in either case — it just narrows from
"nothing" in the UI and from "everything" on the command line.

**Getting one wired up:** right-click the chunk whose output you want to see and choose
**Set View**. That creates a View chunk, wires it to that chunk's output, and makes it
active — one click, not three separate steps. If that chunk already feeds a view, the same
menu entry shows as **Activate View N** instead.

A quick example: build Add Mesh → Transform, right-click Transform, choose **Set View**.
Nudge the Transform's sliders and the viewport updates — nothing upstream of Add Mesh had
to be touched to make that happen, because nothing else was ever asked to run.

**Switching between views:** a graph can hold up to three at once, numbered 1–3. Press `1`,
`2` or `3` to jump between whichever already exist — pressing a number nothing is wired to
does nothing, so you can never accidentally blank the graph.

A view that isn't currently active is dimmed; one whose upstream changed since it last
cooked goes amber, so a stale result never quietly looks current. Each graph *context* —
the top level, or inside a [ChunkGroup](#chunkgroup) — has its own three slots,
independent of every other context's, and a saved file reopens on whichever view was
active when you saved it.

## ChunkGroup

An empty box you build inside. Drop one, double-click to dive in, and work exactly as you
would at the top level. The only param ChunkGroup itself carries is `doc` — a text field
describing what's inside, for whoever opens the file without diving in (a teammate, or an
LLM).

A few things worth knowing before you build one:

- **It's real USD nesting, not a fake container.** Contents live directly under the
  group's own prim path — `/chunks/<Group>/<Child>`, no extra intermediate segment — so the
  outer graph stays uncluttered.
- **The group's output is whatever its own View resolves to, inside it.** Switch which of
  the group's internal views is active, and the group node's output on the outer graph
  changes right along with it.
- **A param you want on the outside gets *lofted*, not referenced.** Lofting makes an
  inner param appear as a real param on the group node itself; the connection points *up*,
  from the inner param to the outer one — not sideways, the way a normal `@Node.param@`
  wire between two chunks does. Treat a loft as its own thing.
- **Groups save as files** and load back by sublayer, reference or payload — plain USD
  composition, nothing bespoke. A chunk that arrives that way shows up **locked**: amber
  header, can't be dragged (auto-layout treats it as a fixed obstacle) — but its params are
  still yours to override, landing as an `over` in your own edit target rather than
  touching the shared file.

Under the hood, `GroupFlags.CONTEXT` is what makes a chunk render as a workspace holding
other chunks rather than as a node itself — the top-level tab and ChunkGroup both carry
it (View itself doesn't; it's an ordinary terminal chunk, not a container). In the Qt
frontend a CONTEXT chunk renders as a tab. See [Writing a graph
frontend](graph-frontends.md) if you're building that rendering for a different host.
