# USD Chunks

usd_chunks is a toolkit for quickly creating ui plugins for USD. Initially intended for UsdView, this project aimed to make it easier for users to add custom tooling to UsdView, without the hassle of writing the UI logic and plugin registratioon each time. usd_chunks solves this by being a usd plugin itself, and loading the chunks (.py files) at runtime from specified locations.

New here? [Install](install.md) covers `pip install` and every environment variable,
including where your chunk modules need to live. [Using the node graph](node-graph-basics.md)
walks through creating and wiring nodes. [Writing a chunk](writing-a-chunk.md) is the full
guide to a chunk module's `main()`, `config()`, `execute()` and `commit()`. [View and
ChunkGroup](core-chunks.md) covers the two chunks that shape a graph rather than doing
anything to the stage themselves.

A Chunk is simply a python object with some information attached, and these are what the ChunkRegistry will look for when regirstering the chunks. The ChunkRegistry will load any .py file specified and run it's main() function. The main() function returns a Chunk objects and the ChunkRegistry will add it to the list of loaded chunks.

A simple example for creating a button with a callback

``` py
from usd_chunks import chunk


def clickedButton(owner, notice, sender):
    print("Button clicked: ", owner.name)


def main():
    # An example of creating a button with a callback
    button = chunk.Button("Button")
    button.add_callback(clickedButton)

    # Create the chunk and add the button parameter
    myChunk = chunk.Chunk("My Chunk")
    myChunk.add_param(button)

    # main returns the chunk object for the ChunkRegistry to use
    return myChunk

```

`main()` takes no arguments. The callback above uses the `(owner, notice, sender)` form,
where `owner` is the object `add_callback` was called on — here, the button. A callback may
instead declare `(notice, sender)`, matching USD's `Tf.Notice`; it receives whichever of the
two it asks for.


The ChunkRegistry will scan files and nested folders inside the directories defined in the environment variable **USD_CHUNK_DIRS**
## Building a chunk at runtime

A chunk does not have to come from a file. You can build one in the interpreter — usdview's
Python panel, a script, a DCC's shell — and add it to the scene:

``` py
from usd_chunks import chunk

s = chunk.get_scene()
c = chunk.Chunk("hello")
s.add_chunk(c)
c.add_param(chunk.Slider("slider"))
```

The chunk appears in the graph immediately, wires up like any other, and its params behave
the same. It is the quickest way to try an idea before it is worth a module.

### What a save keeps

A chunk with no module of its own is recorded as the `runtime` type, and comes back with
its **name, params, values, wiring and position** intact. Params are rebuilt from the
attributes on its prim, so this survives a reopen with nothing else to do:

``` py
c.add_param(chunk.Slider("slider"))   # untouched, and still there next time
```

**Behaviour is not saved.** Callbacks are Python objects belonging to the session that made
them, and a file records no code — deliberately, so that opening a network can never run
any. A runtime chunk therefore reloads as its shape and its data; a chunk that *does*
something needs its code re-run to do it again.

One more limit worth knowing: an attribute records a param's type and value, not which
widget it was built as. A `Slider` of floats comes back as whatever a float maps to, without
its range or label. Names, values and wiring are exact.

When a chunk is worth keeping, move it into a `.py` file on `USD_CHUNK_DIRS` with a `main()`
that builds it — then its behaviour comes back too, because the code is what is loaded.
