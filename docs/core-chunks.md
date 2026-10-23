# Output, View and ChunkGroup

These chunks don't change the stage themselves. They control *when* things cook and
*how* a graph is organised. All are in
[`chunks/core/`](https://github.com/HuginStudios/usd_chunks/tree/main/chunks/core):
[`output.py`](https://github.com/HuginStudios/usd_chunks/blob/main/chunks/core/output.py),
[`view.py`](https://github.com/HuginStudios/usd_chunks/blob/main/chunks/core/view.py) and
[`chunkgroup.py`](https://github.com/HuginStudios/usd_chunks/blob/main/chunks/core/chunkgroup.py).

## 🔎 At a glance

- **Output** is what a graph delivers: the end of its chain. `usdchunks run` cooks to it,
  and so does the graph whenever no View is active. Press `1` to cook to it, or
  right-click a chunk and choose **Set Output** to end the graph there.
- **View** is where you look while working. Right-click a chunk and choose **Set View 2**
  or **Set View 3** to look at it.
- **ChunkGroup** is a subnet. Double-click its header to go inside and build like in any
  other graph. Click the path at the top-left of the graph to come back out.

The rest of this page goes into more detail on each.

## Output

Each graph level (the top level, or the inside of a [ChunkGroup](#chunkgroup)) has one
Output, wired to the end of its chain. A graph with neither an Output nor a View gets an
Output added and connected for you. Only the chunks connected to it cook: anything not
upstream of the Output doesn't run until you wire it back in. Like a View, it has no
output port, so nothing can be wired past it. A file saved before Outputs existed keeps
its Views and gets no Output added.

- **A View wins while it's active.** Looking at a chunk mid-chain cooks only what that
  View needs. With no active View, the graph cooks to its Output.
- **Picking the Output works like picking a View.** Press `1`, or double-click the
  Output's header, to cook to it. Right-click a chunk and choose **Set Output** to move
  the Output's wire to that chunk; the graph then cooks to it. The Output is labelled
  **Output [1]**: blue while the graph cooks to it, dimmed while a View is active.
- **`usdchunks run` always cooks to the Output**, whichever View the file was saved with.
  A file with no Output falls back to its active View.
- **A ChunkGroup delivers its Output** to the outer graph. Its inner Views are for
  looking while you're inside it.

The same split exists elsewhere: Nuke's Viewer and Write, Katana's view flag and Render
node, Houdini's display flag and Output LOP.

### Where results land: C-LIVRPS

What a chunk cooks goes under the session layer, the strongest layer of the stage. So a
cooked result beats every value authored by hand, in any layer: think of it as a **C** in
front of USD's LIVRPS strength order.

```
C   chunk results   a later layer's turn beats an earlier one; nearer the Output beats further
L   local layers    shot.usda > lighting.usda > layout.usda ...
I V R R P S         as USD
```

To change a result, change what the chunk cooks from: its params, which you can override
from any layer, or a chunk after it. When several layers bring chunks (a shot sublayering
its departments), each layer's chunks cook in turn, weakest layer first, ending at that
layer's Output. An Edit Target chunk opts its branch out of C and puts
the results into the layer it names, at that layer's place in L.

## View

**In the UI**, only chunks connected to the active View cook. **From the command line**,
`usdchunks run` cooks to the [Output](#output) instead. A file with neither an Output nor a
view cooks *everything*, and `--all` cooks everything regardless.

**Adding a view:** right-click the chunk whose output you want to see and choose **Set
View 2** or **Set View 3**. If that view already exists it's reconnected to this chunk; otherwise it's
created and connected. Either way it becomes active. The slot already looking at the
chunk is shown ticked.

For example: build Mesh → Transform, right-click Transform and choose **Set View 2**.
Move the Transform's sliders and the viewport updates. Only Mesh and Transform cook,
because nothing else is connected to the view.

**Switching between views:** a graph can have up to two views, numbered 2 and 3, since 1
is the [Output](#output). Press `2` or `3` to switch to a view, and `1` to go back to the
Output. Pressing a number with nothing behind it does nothing, so you can't blank the
graph by accident.

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
- **The group's output comes from its Output.** A group with no Output (an older file)
  falls back to the active View inside it, then to the end of its chain.
- **To show an inner param on the outside, loft it.** Lofting adds the inner param as a
  real param on the group node. The connection goes *up*, from the inner param to the group
  node. It's not the same as a normal `@Node.param@` link between two chunks, so treat
  lofts separately.
- **To show a whole chunk on the outside, loft the chunk.** Right-click it and choose
  **Loft up chunk**. Its full panel then shows on the group's Params page, and in the
  group's section on the page above. Unlike lofting a param, it adds no params or ports to
  the group: it's one `chunk:lofted` flag on the chunk's prim. **Unloft chunk** takes it
  back off.
- **Groups can be saved as files** and loaded back as a sublayer, reference or payload,
  using plain USD composition. A chunk loaded this way is **locked**: it has an amber header
  and can't be moved (auto-layout works around it). You can still change its params. Your
  changes are written as an `over` in your own edit target, and the shared file isn't
  touched.

A chunk with `GroupFlags.CONTEXT` is shown as a workspace that holds other chunks, not as a
node. The top-level tab and ChunkGroup both have it. Output and View don't: they're
normal chunks at the end of a chain, not containers. In the Qt frontend, a CONTEXT chunk is shown as a tab.
See [Writing a graph frontend](graph-frontends.md) if you're building this for another
host.
