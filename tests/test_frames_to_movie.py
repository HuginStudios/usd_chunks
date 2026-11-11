"""Frames to Movie finds a rendered sequence on disk and encodes it into one movie."""

from __future__ import annotations

import re
import subprocess

import pytest
from pxr import Usd

from usd_chunks.chunk_script import Scene

imageio_ffmpeg = pytest.importorskip("imageio_ffmpeg")
Image = pytest.importorskip("PIL.Image")


@pytest.fixture
def scene():
    stage = Usd.Stage.CreateInMemory()
    s = Scene(stage, "pytest")
    yield s
    s.close()


def _write_frames(folder, frames, size=(33, 17), name="render.{:04d}.png"):
    folder.mkdir(parents=True, exist_ok=True)
    for frame in frames:
        Image.new("RGB", size, (frame * 20 % 256, 80, 160)).save(folder / name.format(frame))


def _probe(movie):
    """(frame count, width, height, codec name) of a movie, decoded by the same ffmpeg."""
    result = subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", str(movie), "-map", "0:v:0", "-f", "null", "-"],
        capture_output=True, text=True, errors="replace", check=False,
    )
    assert result.returncode == 0, result.stderr
    stream = re.search(r"Video: (\w+).*?, (\d+)x(\d+)", result.stderr)
    counts = re.findall(r"frame=\s*(\d+)", result.stderr)
    return int(counts[-1]), int(stream.group(2)), int(stream.group(3)), stream.group(1)


def test_make_movie_encodes_every_frame_of_an_odd_sized_sequence(scene, tmp_path):
    _write_frames(tmp_path / "frames", range(1001, 1006))
    movie = tmp_path / "out" / "shot.mp4"
    node = scene.add("frames_to_movie", frames=str(tmp_path / "frames" / "render.####.png"),
                     output_movie=str(movie), make_on_commit=False)

    node.get_param("make_movie").click()

    assert movie.is_file(), node.get_param("status").value
    frames, width, height, codec = _probe(movie)
    assert (frames, codec) == (5, "h264")
    assert (width, height) == (34, 18)  # padded to even sizes
    assert node.get_param("output").value == str(movie)
    assert node.get_param("status").value == "5 frames -> shot.mp4"


def test_commit_makes_the_movie_and_a_gap_in_the_frames_still_works(scene, tmp_path):
    _write_frames(tmp_path, [1, 2, 4, 7])
    movie = tmp_path / "gappy.mp4"
    scene.add("frames_to_movie", frames=str(tmp_path / "render.####.png"), output_movie=str(movie))

    scene.commit()

    assert _probe(movie)[0] == 4


def test_a_path_to_one_frame_finds_the_whole_sequence(scene, tmp_path):
    """Render's Output is the last frame it wrote, so wiring it in must still work."""
    _write_frames(tmp_path, range(3))
    movie = tmp_path / "one.mp4"
    node = scene.add("frames_to_movie", frames=str(tmp_path / "render.0002.png"),
                     output_movie=str(movie), make_on_commit=False)

    node.get_param("make_movie").click()

    assert _probe(movie)[0] == 3


def test_prores_writes_a_mov(scene, tmp_path):
    _write_frames(tmp_path, range(2))
    node = scene.add("frames_to_movie", frames=str(tmp_path / "render.####.png"),
                     output_movie=str(tmp_path / "edit.mp4"), codec="ProRes 422 (.mov)", make_on_commit=False)

    node.get_param("make_movie").click()

    movie = tmp_path / "edit.mov"
    assert node.get_param("output").value == str(movie)
    assert _probe(movie)[3] == "prores"


def test_no_frames_says_so_and_writes_nothing(scene, tmp_path):
    movie = tmp_path / "out" / "none.mp4"
    node = scene.add("frames_to_movie", frames=str(tmp_path / "render.####.png"),
                     output_movie=str(movie), make_on_commit=False)

    node.get_param("make_movie").click()

    assert node.get_param("status").value.startswith("No frames found")
    assert not movie.exists()
    assert not movie.parent.exists()
    assert not node.get_param("output").value


def test_make_movie_on_commit_off_writes_nothing(scene, tmp_path):
    _write_frames(tmp_path, range(2))
    movie = tmp_path / "off.mp4"
    scene.add("frames_to_movie", frames=str(tmp_path / "render.####.png"),
              output_movie=str(movie), make_on_commit=False)

    scene.commit()

    assert not movie.exists()
