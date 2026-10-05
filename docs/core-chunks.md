# View and ChunkGroup

These two chunks don't change the stage themselves. They control *when* things cook and
*how* a graph is organised. Both are in
[`chunks/core/`](https://github.com/HuginStudios/usd_chunks/tree/main/chunks/core):
[`view.py`](https://github.com/HuginStudios/usd_chunks/blob/main/chunks/core/view.py) and
[`chunkgroup.py`](https://github.com/HuginStudios/usd_chunks/blob/main/chunks/core/chunkgroup.py).

## 🔎 At a glance

- **View** decides what's shown and what runs. A graph with no View gets one
  automatically. After that, right-click a chunk and choose **Set View 1** or **Set View 2** to move it there.
- **ChunkGroup** is a subnet. Double-click its header to go inside and build like in any
  other graph. Click the path at the top-left of the graph to come back out.

The rest of this page goes into more detail on both.

## View

**In the UI**, only chunks connected to the active View cook. **From the command line**,
`usdchunks run` does the same, with two differences: a file with no view cooks
*everything*, and `--all` cooks everything even when there is a view.

**Adding a view:** if a graph has no View, one is added and connected for you. After
that, right-click the chunk whose output you want to see and choose **Set View 1** or **Set
View 2**. If that view already exists it's reconnected to this chunk; otherwise it's
created and connected. Either way it becomes active. The slot already looking at the
chunk is shown ticked.

For example: build Mesh → Transform, right-click Transform and choose **Set View 1**.
Move the Transform's sliders and the viewport updates. Only Mesh and Transform cook,
because nothing else is connected to the view.

**Switching between views:** a graph can have up to two views, numbered 1 and 2. Press
`1` or `2` to switch between the ones that exist. Pressing a number with no view does
nothing, so you can't blank the graph by accident.

The active view has an orange header, and the others are dimmed. Each graph level (the top
level, or the inside of a [ChunkGroup](#chunkgroup)) has its own two views. When you
open a saved file, the view that was active when you saved is active again.

## ChunkGroup

A ChunkGroup is an empty box you build inside. Add one, double-click to go inside, and work
the same way as at the top level. Its only param is `doc`, a text field for describing
what's inside, so someone opening the file (a teammate, or an AI) doesn't have to open the
group to find out.

Good to know before you build one:

- **It's real USD nesting.** The chunks inside are stored directly under the group's prim,
  at `/chunks/<Group>/<Child>`, so the outer graph stays clean.
- **The group's output comes from the active View inside it.** Switch which view is active
  inside the group, and the group's output on the outer graph changes to match.
- **To show an inner param on the outside, loft it.** Lofting adds the inner param as a
  real param on the group node. The connection goes *up*, from the inner param to the group
  node. It's not the same as a normal `@Node.param@` link between two chunks, so treat
  lofts separately.
- **Groups can be saved as files** and loaded back as a sublayer, reference or payload,
  using plain USD composition. A chunk loaded this way is **locked**: it has an amber header
  and can't be moved (auto-layout works around it). You can still change its params. Your
  changes are written as an `over` in your own edit target, and the shared file isn't
  touched.

A chunk with `GroupFlags.CONTEXT` is shown as a workspace that holds other chunks, not as a
node. The top-level tab and ChunkGroup both have it. View doesn't: it's a normal chunk at
the end of a chain, not a container. In the Qt frontend, a CONTEXT chunk is shown as a tab.
See [Writing a graph frontend](graph-frontends.md) if you're building this for another
host.
