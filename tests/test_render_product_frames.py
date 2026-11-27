"""A render product named render.####.exr gets one file a frame.

FrameRecorder writes a product to its productName as it stands, read at the default time, so a
frame range rendered through products wrote one file called render.####.exr, each frame over the
last -- and the Render chunk reported render.0001.exr, render.0002.exr ... as written. The chunk
now names the product for the frame (in the session layer) while that frame records.
"""

from __future__ import annotations

import importlib

from pxr import Usd, UsdRender

render = importlib.import_module("chunks.usd.render")


def _stage(product_name):
    stage = Usd.Stage.CreateInMemory()
    settings = UsdRender.Settings.Define(stage, "/Render/settings")
    product = UsdRender.Product.Define(stage, "/Render/frames")
    product.CreateProductNameAttr(product_name)
    settings.CreateProductsRel().SetTargets([product.GetPath()])
    return stage, product.GetProductNameAttr()


def test_a_numbered_product_is_named_for_the_frame_while_it_records(tmp_path):
    stage, name = _stage((tmp_path / "frame.####.exr").as_posix())
    with render._product_names_at(stage, "/Render/settings", 7):
        assert str(name.Get()).replace("\\", "/").endswith("/frame.0007.exr")
    assert str(name.Get()).endswith("frame.####.exr")
    assert not stage.GetSessionLayer().GetAttributeAtPath(name.GetPath())


def test_a_product_with_no_frame_in_its_name_is_left_alone(tmp_path):
    stage, name = _stage((tmp_path / "still.exr").as_posix())
    with render._product_names_at(stage, "/Render/settings", 7):
        assert str(name.Get()).endswith("still.exr")
    assert not stage.GetSessionLayer().GetAttributeAtPath(name.GetPath())


def test_a_session_name_already_there_comes_back(tmp_path):
    stage, name = _stage((tmp_path / "frame.####.exr").as_posix())
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        name.Set((tmp_path / "override.{frame}.exr").as_posix())
    with render._product_names_at(stage, "/Render/settings", 3):
        assert str(name.Get()).endswith("override.3.exr")
    assert str(name.Get()).endswith("override.{frame}.exr")
