# Using the node graph

## 🚀 Your first chunk node

1. Open usdview. The graph is the **Graph** tab in the **Chunks** panel.
2. **Right-click the empty canvas** and pick a chunk, for example *Mesh*. If the graph has
   no View yet, one is added and connected for you.
3. Change the node's params in its panel and watch the viewport update.
4. To see the output of another chunk you add later, right-click it and choose **Set
   View**. A View is only added for you when the graph has none.

That's all you need for a working chunk. The rest of this page covers connecting several
nodes, keyboard shortcuts, disabled and locked nodes, and working inside a ChunkGroup. For
how the graph is stored (prims, ports, USD connections), see [The graph is
USD](index.md#the-graph-is-usd). For more on View and ChunkGroup, see [View and
ChunkGroup](core-chunks.md).

## Opening the graph

The **Graph** tab shows the graph you're currently in: the top level, or the inside of a
[ChunkGroup](core-chunks.md#chunkgroup) if you're in one. Each graph remembers which of its
views was active when you saved, and opens on that one.

## Creating a node

Right-click the empty canvas to open the create menu. Chunks are sorted by the `category`
set in each module's `config()` (see [Writing a chunk](writing-a-chunk.md#config)). Pick
one and it's added at the cursor.

If you drag a wire out of **any port, input or output,** and let go on empty canvas, the
same menu opens. The chunk you pick is created and connected to the port you dragged from.
The menu isn't filtered, so you can pick a chunk with no matching port. In that case the
node is created without a wire, and no error is shown.

## Wiring

Drag from one port to another port of the same type to connect them. A few things work
differently:

- **Drop a node onto an exec wire to insert it.** The exec wire is the chain wire between
  `outputs:chunk` and `inputs:chunk`. It's split and goes through the node.
- **Shake a node quickly back and forth to disconnect all its wires.** The exec chain is
  joined back up behind it: whatever fed the node now feeds what the node used to feed, so
  taking a step out of the middle doesn't break the rest. Value wires are just removed.
- Connecting a value port writes a real `@Node.param@` expression, so it's an actual data
  connection, not just a line on screen (see [The graph is
  USD](index.md#the-graph-is-usd)).

## Nothing cooks without a View

Only chunks connected to the active [View](core-chunks.md#view) cook. A new chain does
nothing until it's connected to one.
**Right-click the chunk whose output you want to see and choose Set View.** That creates a
View, connects it to the chunk's output, and makes it active. If the chunk is already
connected to a view, the menu shows **Activate View N** instead. Until then, the nodes are
set up but don't run. This is on purpose, not a bug.

A graph can have up to three views. Press `1`, `2` or `3` to switch the active one. It only
switches to a number that has a view in the current graph, so you can't blank the graph by
accident.

## Keyboard shortcuts

| Key | Does |
| --- | --- |
| `F` | Frame the selection (or everything, if nothing is selected). |
| `1` / `2` / `3` | Switch to that view, if it exists. |
| `L` | Auto-layout the selection, or the whole graph if nothing is selected. |
| `C` | Collapse the selection, or the whole graph if nothing is selected. |
| `D` | Disable or enable the selection. |
| `Delete` | Delete the selection. |
| `Esc` | Cancel a wire drag, or clear focus. |
| `Tab` | Open the create menu at the cursor. |
| `Ctrl+C` / `Ctrl+X` / `Ctrl+V` | Copy / cut / paste the selection. |
| `Ctrl+D` | Duplicate the selection. |
| `Ctrl+G` | Put the selection into a new [ChunkGroup](core-chunks.md#chunkgroup). |

Some keys do one thing on their own and another with `Ctrl` (`D`/`Ctrl+D`, `C`/`Ctrl+C`).
The plain key is the graph action, and the `Ctrl` version is the usual clipboard action.

## Disabled and locked nodes

A **disabled** chunk (`D`) stays on the canvas, greyed out, so you can turn it back on. It
doesn't cook or produce output.

A **locked** node comes from a layer you don't own, such as a
[ChunkGroup](core-chunks.md#chunkgroup) loaded as a reference, sublayer or payload. It has
an amber header and can't be moved, not even by auto-layout, which works around it. You
can still edit its params. Your edits are written as an `over` in your own edit target, so
a shot can change a shared sequence's params without changing the sequence file.

## Going inside a ChunkGroup

Double-click a ChunkGroup node to go inside and work on its contents as a separate graph,
with its own views. New chunks you create while inside a group are added to that group.
See [ChunkGroup](core-chunks.md#chunkgroup) for how params are shared with the outside
(lofting).
