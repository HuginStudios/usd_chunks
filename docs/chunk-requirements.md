# Chunks that need pip packages

A chunk module can import third-party packages at its top level, as long as it declares them
in `config()`:

``` py linenums="1"
from usd_chunks import chunk

import numpy as np
import cv2 as cv


def show_plot(owner, notice, sender):
    """Draw a two-segment line from the chunk's three sliders."""

    def height(name):
        return int((1 - owner.get_param(name).value / 10) * 511)

    a, b, c = height("A_param"), height("B_param"), height("C_param")
    img = np.zeros((512, 512, 3), np.uint8)
    cv.line(img, (0, a), (256, b), (255, 0, 0), 5)
    cv.line(img, (256, b), (511, c), (255, 0, 0), 5)
    cv.imshow("requirements example", img)


def main():
    node = chunk.Chunk("Req")
    for name in ("A_param", "B_param", "C_param"):
        node.add_param(chunk.Slider(name, flags=chunk.ChunkFlags.ParamFlags.LABELED))
    button = chunk.Button("plot", "Plot")
    node.add_param(button)
    node.add_callback(show_plot, sender=button)
    return node


def config():
    return chunk.DefaultConfig(
        chunk.StandardCategory.EXAMPLES,
        "Plots three sliders with OpenCV.",
        requirements=["opencv-python>=4,<5", "numpy>=2,<3"],
    )
```

## How it works

The registry reads `config()` straight from the source, without running the module, so the
requirements are known even when the packages are missing and `import cv2` would fail.

When a chunk is loaded with packages missing, the host asks once for all of them, naming
the chunk: usdview shows "OK to install requests for chunk AMDMaterialXLibrary?" with
Yes/No, `usdchunks run` asks the same on an interactive terminal, and Blender shows a popup
once the add-on has loaded (one checkbox per chunk when several need something).

- **Yes:** they are installed into `USD_CHUNK_DEPENDENCY_DIR` (a subfolder per Python
  version), with pip's progress shown: a progress dialog in usdview, pip's output in the
  terminal, the status bar and USDChunks sidebar in Blender. That directory is on
  `sys.path`, and then the module body runs.
- **No, or nobody to ask** (Blender, CI, a piped run): nothing is installed. The chunk does
  not load, and a warning names the missing packages and the pip command to install them.
  A "no" is remembered until restart, so a network with ten of that chunk asks once.

Opening someone else's network never adds packages without your yes.
`USD_CHUNK_AUTO_INSTALL` changes the default: `1` installs without asking, `0` never installs
and only warns.

## Declare a range, not a bare name

Write each requirement with a version range, such as `numpy>=2,<3`. A bare `numpy` still
works, but you get a warning: two runs a month apart could use different libraries from the
same network, and nothing recorded would explain the difference. An exact pin (`==`) is
also discouraged, because two chunks pinning different versions of one package cannot both
be satisfied.

Declare every third-party package the module imports. An undeclared import is an install that
breaks on a clean machine, and a version that never reaches a run's manifest.
`usdchunks deps` lists chunks with undeclared or unconstrained requirements, and
`usdchunks deps --strict` fails on them, for CI.
