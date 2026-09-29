# Writing a graph frontend

The node graph is split in two. **The engine** (`usd_chunks.graph.engine`) decides
*what happens to the graph*; **a frontend** decides *what appears on screen* and how input
arrives. The engine imports no toolkit — a test asserts that in a subprocess — so a
frontend can be written for any of them.

There are four today, deliberately different in shape:

| | `frontends.qt.nodegraph` | `frontends.flet.nodegraph` | `frontends.imgui.nodegraph` | `frontends.blender.nodegraph` |
|---|---|---|---|---|
| Drawing | `QGraphicsItem`s that place themselves | canvas primitives recomputed each refresh | redrawn every frame | **none** — Blender draws the tree |
| Node bodies | real `ChunkWidget`s via proxy widgets | real Flet controls in a `Stack` | drawn inline per frame | RNA properties, on their input socket (rest via `draw_buttons`) |
| Hit-testing | the scene does it | the view does it (`port_at`, `node_at`) | `imgui_node_editor` does it | Blender does it |
| Positions | on the item, written back to USD | on the view, written back to USD | owned by the editor once placed | on the node, pulled back on save |
| Implements the contract | through an adapter (`QtGraphFrontend`) | directly on `GraphView` | directly on `ImGuiGraphView` | directly on `BlenderGraphView` |

Pick the closest model and read it first. Retained/scene-based toolkits look like Qt;
immediate-mode looks like ImGui; Flet sits between the two. If your host already *has* a
node editor, read Blender — it is the case where the frontend supplies the seventeen verbs
and draws nothing at all, which is the clearest illustration that the engine is not
secretly shaped around a canvas.

## What you get for free

Everything in this list already works and must not be reimplemented.

**Reading**

- `read_graph(stage, context, chunks)` — prims, ports and edges for one context.
- `contexts(stage)` / `default_context(stage)` — which graphs a file holds, and which one
  to open. A file whose top level holds one chunkgroup is asking to be opened *inside* it;
  reading at `CHUNKS_ROOT` shows one node and none of the work.
- [`PortRef`][usd_chunks.graph.engine.PortRef], `iter_ports`, `param_port_type`,
  `sdf_to_port_type` — ports and their typing.
- `node_position` / `graph_position` / `write_position` — see *Coordinates* below.

**Authoring**

- `can_connect`, `connect`, `disconnect`. Never author a connection yourself; `connect`
  also writes the `@Node.param@` expression that makes a typed wire resolve.
- `create_from_entry(scene, entry, usd_pos, nest)` — the create sequence, including the
  position hook and the narrow exception set. It returns `(created, error)`.
- `paste_nodes(...)` and `rewire_pasted(...)` — materialize a clipboard selection and
  re-wire it. `rewire_pasted` reads ports off the stage, so it works before a reload.
- `group_into_chunkgroup(...)` — the whole group operation: read the boundary, snapshot,
  delete, make the group, paste inside, wire it back into the chain.
- `delete_chunks`, `toggle_disabled`, `collapse_or_expand`, `apply_selection`, `chunks_for`.

**Clipboard**

`ClipboardNode`, `ClipboardEdge`, and the module-level `node_clipboard` / `edge_clipboard`,
via `copy_to_clipboard(nodes, edges)`. These are **shared between frontends** — a selection
copied in one can be pasted in another — so assign *into* the lists, never rebind them.

**View slots**

`read_active_slot`, `write_active_slot`, `activate_view_slot`, `new_view_slot`,
`default_view_slot`. A view is what drives execution: `chunk_stage.recook_for_view` cooks the
active view's upstream tree and blocks everything else, so **a context with no view cooks
nothing at all**. Call `default_view_slot` when you open a graph. Every non-Qt frontend
shipped without it once and looked correct while doing nothing.

**Navigation and layout**

`can_dive`, `parent_context`, `nest_target`, `menu_entries`, `exec_boundary`,
`layout_adjacency`, `layout_graph`, and the algorithm in `chunk_graph_layout`.

## What you must implement

[`GraphFrontend`][usd_chunks.graph.engine.GraphFrontend] names seventeen verbs:

```
set_selection      selected_chunks
delete_selected    toggle_disable_selected    collapse_selected_or_all
copy_selection     cut_selection      paste_at    duplicate_selection
group_selection    create_chunk
can_dive           dive_into          dive_out
active_slot        set_active_slot
frame
```

It is a `Protocol`, not a base class, so there is nothing to inherit. Add your class to
`frontends()` in `tests/test_graph_frontend_contract.py` on day one — **the failures are
your to-do list**, and the test checks signatures as well as names.

Most of these are now three or four lines: call the engine, then save, reload and set a
status. An engine function never reloads, never saves and never sets a status — it returns
what you need in order to do those things the way your toolkit does them. If your
implementation of a verb is thirty lines, check whether you are rewriting something above.

Drawing and input are deliberately *not* in the contract. They have no shared shape, and a
test asserts they stay out.

## Coordinates

USD is Y-up. Most canvases are Y-down. `graph_position(prim)` returns canvas space and is
the only correct way to read a node position for drawing; `write_position(prim, x, y)`
takes the coordinates USD stores, so convert on the way in as you converted on the way out.

Blender is the exception that proves the rule: its node editor is Y-up like USD, so its
frontend passes positions straight through — and consequently its `duplicate_selection`
offsets by `-40` where the others use `+40`. The conversion is yours; the authoring is not.

This is not a detail. The flip originally lived inside a private Qt helper, so the second
frontend had no flip at all and rendered every file **mirrored** — a node dragged in one
jumped to the other side when opened in the other.

## Six things that will bite you

**1. Diving does not change where new chunks land.** `add_chunk` parents to the scene's
current context, which navigating does not set. Wrap creation in
`scene.creating_in(nest_target(...))`, or a node created inside a group silently lands
outside it — and on a brand-new network it lands in the scene's *default* chunkgroup,
which is not the graph you are showing, so the create appears to do nothing at all.

**2. Selection belongs on the chunk, not on your view.** The shared operations read
`ChunkFlags.State.SELECTED`. A selection kept privately is invisible to them, and
`copy_selection` will find nothing. Use `apply_selection`, which writes the bit for you.

**3. Passing the conformance test is not evidence of working.** It checks that the verbs
exist with the right signatures. Every frontend has shipped bugs it could never have
caught: all of Flet's pointer handlers raised `AttributeError` on first touch while looking
perfectly implemented, Qt's clipboard was not actually shared despite correct verbs, and
Blender called a state method that does not exist, in two places, because the tests each
asserted only the direction that happened to work. Write behavioural tests — see
`tests/test_qt_graph_frontend.py` and `tests/test_chunk_graph.py` — and assert **both**
directions of anything that toggles.

**4. Catch narrowly.** The `clear_state` bug above survived because it sat inside a bare
`except Exception`. If you are guarding against "a chunk object that carries no state
bits", catch `AttributeError` and let everything else surface.

**5. Keyboard shortcuts collide with text entry.** If your toolkit dispatches keys at the
window level, a `Delete` meant for a text field will delete the selected node. Flet solves
this with `frontends.flet.factory.FocusTracker`: widgets report focus, and the graph checks before
acting. Qt does not need it because its widgets consume keys first, and Blender and ImGui
do not because their editors own key routing.

**6. `dive_out` may not exist in your toolkit's terms.** Qt navigates by opening a tab, so
"out" is a back-step through the docker's navigation history; the others swap the context
in place. Same verb, genuinely different mechanism. Implement whichever your shell
supports, and make it a documented no-op if it supports neither. Use `parent_context` to
decide *where* out is — it walks up to a path that actually holds chunks, which a single
`rsplit` does not.

## What is deliberately not shared

**Qt's materialization.** The three synchronous frontends share `materialize` and
`paste_nodes`. Qt does not: it defers wiring a paste through a `QTimer` because its ports
arrive in deferred proxy batches, guards a window with `chunk._executing`, and blocks
pasted chunks when the context has an active view. Those steps *are* the difference, so a
shared implementation would be one function with a Qt branch inside it. If your widgets
appear asynchronously, copy Qt; if they appear immediately, call `paste_nodes`.

**Applying a layout.** `layout_graph` computes the positions for everyone. Applying them
does not generalise: Blender must set both the node and the prim, ImGui must tell the
editor because it owns positions from then on, and Flet writes its own dict.

**Framing.** Blender selects and calls `bpy.ops.node.view_selected`, ImGui calls
`navigate_to_content`, Qt unions rects and calls `fitInView`, Flet solves a zoom and a pan
by hand. There is nothing left in common once the toolkit has a viewport.

**Node locking.** Qt and Blender mark nodes whose prim comes from an external or
referenced layer (the rule is the same in both: the strongest non-anonymous, non-root
opinion is not the edit target) and pass `is_locked` to `layout_graph` so auto-layout
routes around them. Qt refuses the drag; Blender cannot, so it drops the dragged position
when writing back and the next reload restores it. Flet and ImGui have no such concept
yet, so a layout there can move a node the other two would have pinned. If your frontend
can express it, implement it.

**Staying honest between reloads.** A frontend whose toolkit owns the editing (Blender)
must also catch what the toolkit does behind its back. Blender's frontend shadows the
stock node operators (delete, duplicate, copy/paste, mute) with the chunk verbs inside its
tree type, diffs the tree's links against the model's edges in `NodeTree.update` so a cut
wire disconnects in USD, listens for chunk notices (value, state, param add/remove,
delete, prim resync) and re-mirrors the touched nodes one tick later, and rebuilds from
USD after every undo/redo -- because Blender's undo restores the `.blend`, never the
stage. A canvas frontend gets all of this for free by being the only writer of its scene.

**Node locking.** Qt marks nodes whose prim comes from an external or referenced layer and
refuses to move them during auto-layout. Blender does the same, dropping the drag on write-back since it cannot refuse one. Flet
and ImGui have no such concept yet, so a layout there can move a node Qt would have
pinned. If your frontend can express it, implement it.
Dive keys on `chunk_stage.is_subnet`: a chunkgroup, or a chunk hosting a referenced network
(a Reference, Sublayer or Payload pointed at a saved network file). A lock is about position, not params: a locked node's params are meant to be edited, and
the edit lands as an `over` in the stage's edit target (`chunk_utils.host_edit_layer`), so a
shot can tweak a sequence's network without touching the sequence file.

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

Two typing traps, both found the hard way in Blender. A `Slider` declares `INT` *and*
`FLOAT` — one class serves both, and `is_int` is what decides; testing the flags in the
wrong order makes every float slider an integer one, and the symptom is a param that
silently fails to mirror rather than an error. And containers (`Vector`, `Group`) hold the
params you actually want: skipping anything without a `get_type` gives a node with a port
for `tz` and no control for it.
