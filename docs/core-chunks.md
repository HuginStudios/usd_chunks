# View and ChunkGroup

These two chunks don't change the stage themselves. They control *when* things cook and
*how* a graph is organised. Every other chunk in
[`chunks/`](https://github.com/HuginStudios/usd_chunks/tree/main/chunks) does something to
your scene. Both are in
[`chunks/core/`](https://github.com/HuginStudios/usd_chunks/tree/main/chunks/core):
[`view.py`](https://github.com/HuginStudios/usd_chunks/blob/main/chunks/core/view.py) and
[`chunkgroup.py`](https://github.com/HuginStudios/usd_chunks/blob/main/chunks/core/chunkgroup.py).

## 🔎 At a glance

- **View** decides what's shown and what runs. The first chunk in a graph gets one
  automatically. After that, right-click a chunk and choose **Set View** to move it there.
- **ChunkGroup** is a subnet. Double-click it to go inside and build like in any other
  graph, and double-click again to come back out.

The rest of this page goes into more detail on both.

## View

**In the UI**, nothing cooks unless it's connected to a View. Only what the active view
needs is cooked. **From the command line** it's the other way around: `usdchunks run`
cooks *everything* in a file that has no view, and `--all` cooks everything even when there
is one. Either way, a view limits what runs.

**Adding a view:** the first chunk in an empty graph gets a View added and connected for
you. For later chunks, right-click the one whose output you want to see and choose **Set
View**. That creates a View, connects it to the chunk's output, and makes it active. If the
chunk is already connected to a view, the menu shows **Activate View N** instead.

For example: build Add Mesh → Transform, right-click Transform and choose **Set View**.
Move the Transform's sliders and the viewport updates. Only Add Mesh and Transform cook,
because nothing else is connected to the view.

**Switching between views:** a graph can have up to three views, numbered 1 to 3. Press
`1`, `2` or `3` to switch between the ones that exist. Pressing a number with no view does
nothing, so you can't blank the graph by accident.

A view that isn't active is dimmed. A view whose inputs changed since it last cooked turns
amber, so you can tell its result is out of date. Each graph level (the top level, or the
inside of a [ChunkGroup](#chunkgroup)) has its own three views. When you open a saved file,
the view that was active when you saved is active again.

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
