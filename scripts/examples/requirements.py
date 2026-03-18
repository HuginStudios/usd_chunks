from usd_chunks import chunk, chunk_registry

import numpy as np
import cv2 as cv


def show_plot(owner: chunk.Chunk, notice, sender):
    a = owner.get_dependency(0)
    b = owner.get_dependency(1)
    c = owner.get_dependency(2)

    assert isinstance(a, chunk.Chunk) and isinstance(b, chunk.Chunk) and isinstance(c, chunk.Chunk)

    ap = a.get_param("A_param")
    bp = b.get_param("B_param")
    cp = c.get_param("C_param")

    assert isinstance(ap, chunk.Slider) and isinstance(bp, chunk.Slider) and isinstance(cp, chunk.Slider)

    apv = int((1 - ap.value / 10) * 511)
    bpv = int((1 - bp.value / 10) * 511)
    cpv = int((1 - cp.value / 10) * 511)

    # Create a black image
    img = np.zeros((512, 512, 3), np.uint8)

    # Draw a diagonal blue line with thickness of 5 px
    cv.line(img, (0, apv), (256, bpv), (255, 0, 0), 5)
    cv.line(img, (256, bpv), (511, cpv), (255, 0, 0), 5)

    cv.imshow("hello", img)


def main():
    myChunk = chunk.Chunk("Req")
    myChunk.add_callback(show_plot, notice_alias=chunk.ChunkNoticeAlias.INPUT_CHANGED)

    btn = chunk.Button("matplotlib")
    myChunk.add_param(btn)

    reg = chunk_registry.ChunkRegistry()
    if mod := reg.get_chunk_module("examples.dependencies"):
        if not mod.loaded:
            chunks = mod.load()
        else:
            chunks = mod.create()

        for c in chunks:
            myChunk.add_dependency(c)

    return myChunk


def config():
    return chunk.DefaulConfig(
        chunk.StandardCategory.EXAMPLES,
        "Example showing shunk requirements on external packages. The requirements will be installed if not found in the "
        "current environment",
        requirements=["opencv-python", "numpy"],
    )
