# Using the node graph

## 🚀 Your first chunk node

1. Open usdview — the **Chunks** panel (or the **Chunks** menu) is where the graph lives.
2. **Right-click empty canvas** and pick any chunk, e.g. *Add Mesh*. A View node is
   placed and wired to it automatically — the first chunk in an empty graph always gets
   one, so there's nothing extra to do here.
3. Play with the node's params in its panel and watch the viewport update.
4. Add a second chunk and want to see *its* output instead? Right-click it and choose
   **Set View** — the auto-wiring only happens for the very first node in a graph, so
   this is how you point the view at anything after that.

That's it — you have a working chunk. Everything below is for when you want more: wiring
several nodes together, keyboard shortcuts, disabled/locked nodes, working inside a
ChunkGroup. For what a node graph is *made of* under the hood (prims, ports, USD
connections), see [The graph is USD](index.md#the-graph-is-usd) in the docs home; for View
and ChunkGroup specifically, see [View and ChunkGroup](core-chunks.md).

## Opening the graph

The **Chunks** panel/menu opens the graph view for the current context — the top-level
graph, or, if you're inside a [ChunkGroup](core-chunks.md#chunkgroup), that group's own
inner graph. Every context remembers which of its (up to three) view slots was active
when it was saved, and opens back on that one.

## Creating a node

Right-click empty canvas for the create menu, organized by the `category` each chunk
module declared in its `config()` (see [Writing a chunk](writing-a-chunk.md#config)).
Picking an entry drops that chunk at the cursor.

Dragging a wire out of **any port — input or output** — and releasing it on empty canvas
opens the same create menu, and picking an entry creates the node *and* wires it to the
port you dragged from, in one motion. The menu here isn't filtered to compatible chunks —
it's the same full list either way — so it's possible to pick something with no matching
port; when that happens you get the new node with no wire, silently, rather than an error.

## Wiring

Drag from a port to another port of a matching type to connect them. A few things behave
specially:

- **Dropping a node onto an existing wire splices it in** — the wire is cut and rerouted
  through the new node's matching input/output pair, rather than just landing on top of it.
- **A quick back-and-forth "shake" drag on a node disconnects every wire touching it**, and
  heals the exec chain behind it — whatever fed the node's input now feeds whatever it used
  to feed, so removing a step from the middle of a chain doesn't leave the rest dangling.
- Connecting a value port authors a real `@Node.param@` expression, not just a visual line
  — the connection *is* the dataflow (see [The graph is USD](index.md#the-graph-is-usd)).

## Nothing cooks without a View

A freshly created chain does nothing until it's wired into a [View](core-chunks.md#view)
chunk. **Right-click the chunk whose output you want to see and choose Set View** — that
creates a View chunk, wires it to that chunk's output, and makes it active in one step. If
that chunk already feeds a view, the same menu entry shows as **Activate View N** instead.
Until something is wired to an active view, every node sits there fully configured and
inert; that's deliberate, not a bug you're diagnosing.

A graph can hold up to three views at once. Press `1`, `2` or `3` to switch which one is
active — it only switches to a slot that already has a view in the current graph, so you
can never accidentally blank the whole thing by pressing a number nothing is wired to.

## Keyboard shortcuts

| Key | Does |
| --- | --- |
| `F` | Frame the current selection (or everything, with nothing selected). |
| `1` / `2` / `3` | Switch the active view slot, if one exists there. |
| `L` | Auto-layout the current selection, or the whole graph with nothing selected. |
| `C` | Collapse the current selection, or the whole graph with nothing selected. |
| `D` | Toggle disable on the current selection. |
| `Delete` | Delete the current selection. |
| `Esc` | Cancel an in-progress wire drag, or clear focus. |
| `Tab` | Open the create menu (the chunk picker) at the cursor. |
| `Ctrl+C` / `Ctrl+X` / `Ctrl+V` | Copy / cut / paste selection. |
| `Ctrl+D` | Duplicate selection. |
| `Ctrl+G` | Group the current selection into a new [ChunkGroup](core-chunks.md#chunkgroup). |

Shortcuts that double up a key with its `Ctrl` form (`D`/`Ctrl+D`, `C`/`Ctrl+C`) are
deliberate — the bare key is the graph-specific action, the modified one is the familiar
clipboard verb.

## Disabled and locked nodes

A **disabled** chunk (`D`) stays visible on the canvas, greyed out, rather than
disappearing — it's still there to re-enable, just not cooking or contributing output.

A **locked** node — one whose prim comes from a layer you don't own: a
[ChunkGroup](core-chunks.md#chunkgroup) loaded by reference, sublayer or payload, or any
other externally-authored layer — gets an amber header and can't be dragged, including by
auto-layout, which routes around it as a fixed obstacle. Its params are still meant to be
edited: an edit there lands as an `over` in your own edit target, so a shot can tweak a
shared sequence's params without touching the sequence file itself.

## Diving into a ChunkGroup

Double-click a ChunkGroup node to dive into it and work on its contents as their own
graph, with their own view slots. New chunks created while you're inside a group nest
under it automatically. See [ChunkGroup](core-chunks.md#chunkgroup) for what crosses the
boundary (lofted params) and what doesn't (everything else).
