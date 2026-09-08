# Writing a graph frontend

The node graph is split in two. **The engine** (`usd_chunks.plugins.chunk_graph`) decides
*what happens to the graph*; **a frontend** decides *what appears on screen* and how input
arrives. The engine imports no toolkit — a test asserts that in a subprocess — so a
frontend can be written for any of them.

There are two today, deliberately different in shape:

| | `chunk_qt_nodegraph` | `chunk_flet_nodegraph` |
|---|---|---|
| Drawing | `QGraphicsItem`s that place themselves | canvas primitives recomputed each refresh |
| Node bodies | real `ChunkWidget`s via proxy widgets | real Flet controls in a `Stack` |
| Hit-testing | the scene does it | the view does it (`port_at`, `node_at`) |
| Implements the contract | through an adapter (`QtGraphFrontend`) | directly on `GraphView` |

Read both before starting. If your toolkit is immediate-mode, Flet is the closer model; if
it is retained/scene-based, Qt is.

## What you get for free

Everything in this list already works and must not be reimplemented:

- **Reading a network** — `read_graph(stage, context, chunks)` returns prims, ports and
  edges for one context.
- **Ports and typing** — [`PortRef`][usd_chunks.plugins.chunk_graph.PortRef],
  `iter_ports`, `param_port_type`, `sdf_to_port_type`.
- **Connection rules and authoring** — `can_connect`, `connect`, `disconnect`. Never author
  a connection yourself; `connect` also writes the `@Node.param@` expression that makes a
  typed wire resolve.
- **Positions** — `node_position` (what USD stores) and `graph_position` (what a canvas
  draws; see *Coordinates* below).
- **Clipboard** — `ClipboardNode`, `ClipboardEdge`, and the module-level `node_clipboard`
  / `edge_clipboard`. These are **shared between frontends**: a selection copied in one can
  be pasted in another, so use them rather than inventing a format.
- **Operations** — `snapshot_selection`, `exec_boundary`, `delete_chunks`,
  `layout_adjacency`, and the layout algorithm in `chunk_graph_layout`.
- **Execution** — `chunk_usd.recook_for_view` switches which view drives cooking. Do not
  reimplement it; a highlight is not a view switch.

## What you must implement

[`GraphFrontend`][usd_chunks.plugins.chunk_graph.GraphFrontend] names sixteen verbs:

```
set_selection      selected_chunks
delete_selected    toggle_disable_selected
copy_selection     cut_selection      paste_at    duplicate_selection
group_selection    create_chunk
can_dive           dive_into          dive_out
active_slot        set_active_slot
frame
```

It is a `Protocol`, not a base class, so there is nothing to inherit. Add your class to
`frontends()` in `tests/test_graph_frontend_contract.py` on day one — **the failures are
your to-do list**, and the test checks signatures as well as names.

Drawing and input are deliberately *not* in the contract. They have no shared shape, and a
test asserts they stay out.

## Coordinates

USD is Y-up. A canvas is Y-down. `graph_position(prim)` returns canvas space and is the
only correct way to read a node position for drawing.

This is not a detail. The flip originally lived inside a private Qt helper, so the second
frontend had no flip at all and rendered every file **mirrored** — a node dragged in one
jumped to the other side when opened in the other. Write positions back through the same
conversion you read them with.

## Five things that will bite you

**1. Diving does not change where new chunks land.** `add_chunk` parents to the scene's
current context, which navigating does not set. After a dive you must also wrap creation in
`scene.creating_in(group)`, or a node created inside a group silently lands outside it.
Position is passed the same way: `chunk_usd.creating_at(pos)`.

**2. Selection belongs on the chunk, not on your view.** The shared operations read
`ChunkFlags.State.SELECTED`. A selection kept privately is invisible to them, and
`copy_selection` will find nothing.

**3. Passing the conformance test is not evidence of working.** It checks that the verbs
exist with the right signatures. Both existing frontends have shipped bugs it could never
have caught: every one of Flet's pointer handlers raised `AttributeError` on first touch
while looking perfectly implemented in the source, and Qt's clipboard was not actually
shared despite the verbs being correct. Write behavioural tests too — see
`tests/test_qt_graph_frontend.py`.

**4. Keyboard shortcuts collide with text entry.** If your toolkit dispatches keys at the
window level, a `Delete` meant for a text field will delete the selected node. Flet solves
this with `chunk_flet.FocusTracker`: widgets report focus, and the graph checks before
acting. Qt does not need it because its widgets consume keys first.

**5. `dive_out` may not exist in your toolkit's terms.** Qt navigates by opening a tab, so
"out" is a back-step through the docker's navigation history; Flet swaps the context in
place. Same verb, genuinely different mechanism. Implement whichever your shell supports,
and make it a documented no-op if it supports neither.

## What is deliberately not shared

**Materialization** — turning clipboard entries into live chunks. Qt defers wiring a paste
through a `QTimer` because its ports arrive in deferred proxy batches, guards a window with
`chunk._executing`, and blocks pasted chunks when the context has an active view. Flet
builds synchronously and needs none of it. The steps that differ *are* the difference, so a
shared implementation would be one function with a branch per toolkit. Write your own, and
copy whichever existing one matches your widget lifecycle.

**Node locking.** Qt marks nodes whose prim comes from an external or referenced layer and
refuses to move them during auto-layout. Flet has no such concept yet, so a layout there
can move a node Qt would have pinned. If your frontend can express it, implement it.

## Param widgets

A graph frontend also needs a widget per param type, which is a separate extension point
with its own guide — see [Plugins](plugins.md). Implement
[`ChunkWidgetFactoryBase`][usd_chunks.chunk_widget_api.ChunkWidgetFactoryBase] and one
[`ChunkParamWidgetBase`][usd_chunks.chunk_widget_api.ChunkParamWidgetBase] subclass per
`ChunkFlags.ParamType`.

Cover **every** type in the factory map from the start. A `Menu` param crashed the Flet
frontend on startup for a while because no test network happened to contain one, so that
branch of the factory had never once executed. `tests/test_flet_widgets.py` shows the shape
of the test that prevents it: build every widget the factory can return, from a real param.
