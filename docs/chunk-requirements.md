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

What happens next depends on `USD_CHUNK_AUTO_INSTALL`:

- **Unset (the default):** nothing is installed. A missing package is named in a warning,
  and the chunk does not load. Opening someone else's network can never add packages to
  your environment on its own.
- **Set:** the missing packages are installed, after asking, into
  `USD_CHUNK_DEPENDENCY_DIR`. That directory is added to `sys.path`, and then the module
  body runs.

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
