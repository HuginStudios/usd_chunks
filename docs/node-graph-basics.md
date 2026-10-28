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
USD](index.md#the-graph-is-usd). For more on Output, View and ChunkGroup, see [Output, View and
ChunkGroup](core-chunks.md).

## Opening the graph

The **Graph** tab shows the graph you're currently in: the top level, or the inside of a
[ChunkGroup](core-chunks.md#chunkgroup) if you're in one. Each graph remembers which of its
views was active when you saved, and opens on that one.

## The Params page

The **Params** tab next to it has two boxes.

- **The group's box**, at the top, is the graph's interface: the params and the chunks
  lofted to it (see [ChunkGroup](core-chunks.md#chunkgroup)). To put a chunk there,
  right-click it and choose **Loft up chunk**. A chunk made from the Params tab's own create
  menu is lofted for you, so it shows where you made it. Click the box's title to fold it.
- **The search box**, below it, finds any other chunk in the graph. Type in it and it shows
  the chunks that match and aren't lofted. The line under the search bar says how many it
  found, and how many lofted chunks above match too. Searching never changes the group's box.

## Creating a node

Right-click the empty canvas to open the create menu. Chunks are sorted by the `category`
set in each module's `config()` (see [Writing a chunk](writing-a-chunk.md#config)). Pick
one and it's added at the cursor.

If you drag a wire out of **any port, input or output,** and let go on empty canvas, the
same menu opens. The chunk you pick is created and connected to the port you dragged from.
The menu isn't filtered, so you can pick a chunk with no matching port. In that case the
node is created without a wire, and no error is shown.

To build a chain quickly, tick **Auto-connect** in the bottom-right corner, next to the
Output badge. Each chunk you then make from the create menu is wired after the selected
node. With no node selected, or several, it goes after the one the active Output or View
shows. The Output or View moves onto the new chunk, and the new chunk is selected, so the
next one goes after it. The box is remembered between sessions.

A node's header shows its icon, name and type (to set an icon, see [Icons in the node
graph](writing-a-chunk.md#icons-in-the-node-graph)). The **⋮** button at its right opens
the same menu as right-clicking the node.

To make a node wider or narrower, drag its right edge. Double-click the edge to go back to
the default width. The width is saved in the file, on the chunk prim's
`ui:nodegraph:node:size`.

## Wiring

Drag from one port to another port of the same type to connect them. A port lights up when
the mouse comes near it, to show you can start a wire there. A port's colour always shows
its type of value.

While you drag, the ports you can drop on get a white ring. Near one, the end of the wire
jumps onto it, turns solid, and the port lights up: that's where the wire lands if you let
go. Away from any port, the end follows the mouse again, dashed.

A few things work differently:

- **Drop a node onto an exec wire to insert it.** The exec wire is the chain wire between
  `outputs:chunk` and `inputs:chunk`. It's split and goes through the node.
- **Shake a node quickly back and forth to disconnect all its wires.** The exec chain is
  joined back up behind it: whatever fed the node now feeds what the node used to feed, so
  taking a step out of the middle doesn't break the rest. Value wires are just removed.
- Connecting a value port writes a real `@Node.param@` expression, so it's an actual data
  connection, not just a line on screen (see [The graph is
  USD](index.md#the-graph-is-usd) and [Expressions](expressions.md)).

## Nothing cooks without an Output or a View

Only chunks connected to the active [View](core-chunks.md#view) cook, or with no active
View, the ones connected to the graph's [Output](core-chunks.md#output). A new chain does
nothing until it's connected to one.
**Right-click the chunk whose output you want to see and choose Set View 2 or Set View 3.**
That connects that View to the chunk's output (creating it if needed) and makes it active.
The slot already looking at the chunk is shown ticked. Until then, the nodes are
set up but don't run. This is on purpose, not a bug.

A graph can have up to two views, numbered 2 and 3. Press `2` or `3` to switch to one,
and `1` to go back to the Output. To end the graph at another chunk, right-click it and
choose **Set Output**. A key only switches to something the current graph has, so you
can't blank the graph by accident.

## Keyboard shortcuts

| Key | Does |
| --- | --- |
| `F` | Frame the selection (or everything, if nothing is selected). |
| `Ctrl+F` | Find a node by name or chunk type (untick Name or Type to search only the other). `Enter` frames the best match; `Ctrl+Enter` frames the next one each press. |
| `1` | Cook to the Output. |
| `2` / `3` | Switch to that view, if it exists. |
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
