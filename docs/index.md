# USD Chunks

usd_chunks is a toolkit for quickly creating ui plugins for USD. Initially intended for UsdView, this project aimed to make it easier for users to add custom tooling to UsdView, without the hassle of writing the UI logic and plugin registratioon each time. usd_chunks solves this by being a usd plugin itself, and loading the chunks (.py files) at runtime from speficied locations.

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