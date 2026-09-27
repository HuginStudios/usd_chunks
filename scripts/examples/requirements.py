"""Example: a chunk that depends on external pip packages.

The packages named in config().requirements are installed on demand, the first time the
chunk is loaded, if they are not already importable. That is what makes the top-level
`import numpy` / `import cv2` below safe: the registry reads config() without executing
this module body (see _ConfigTransformer), installs what is missing, and only then lets
the body run.
"""

from usd_chunks import chunk

import numpy as np
import cv2 as cv


def show_plot(owner: chunk.Chunk, notice, sender):
    """Draw a two-segment line from the chunk's own three sliders.

    `owner` is the chunk the callback was added to, not the Button passed as `sender`.
    """

    def height(name: str) -> int:
        param = owner.get_param(name, chunk.is_param_t(float))
        return int((1 - param.value / 10) * 511)

    a, b, cc = height("A_param"), height("B_param"), height("C_param")

    img = np.zeros((512, 512, 3), np.uint8)
    cv.line(img, (0, a), (256, b), (255, 0, 0), 5)
    cv.line(img, (256, b), (511, cc), (255, 0, 0), 5)

    cv.imshow("requirements example", img)


def main():
    my_chunk = chunk.Chunk("Req")

    for name in ("A_param", "B_param", "C_param"):
        my_chunk.add_param(chunk.Slider(name, flags=chunk.ChunkFlags.ParamFlags.LABELED))

    btn = chunk.Button("plot", "Plot")
    my_chunk.add_param(btn)
    my_chunk.add_callback(show_plot, sender=btn)

    return my_chunk


def config():
    return chunk.DefaultConfig(
        chunk.StandardCategory.EXAMPLES,
        "Example showing chunk requirements on external packages. The requirements are "
        "installed if not found in the current environment.",
        requirements=["opencv-python>=4,<5", "numpy>=2,<3"],
    )
