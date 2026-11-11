"""Frames to Movie chunk -- bakes a rendered image sequence into one movie file.

Reads the frames a Render chunk wrote (render.####.png) and runs ffmpeg on them. The movie
is written in commit() and by the Make Movie button, never in execute().
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading

from usd_chunks import chunk, chunk_utils

try:
    import imageio_ffmpeg
except ImportError:  # the chunk still loads; Make Movie says the package is missing
    imageio_ffmpeg = None

logger = logging.getLogger(f"usd_chunks.{__name__}")

#: Codec menu entry -> (file extension, ffmpeg arguments).
_CODECS = {
    "H.264 (.mp4)": (".mp4", ["-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p",
                              "-movflags", "+faststart"]),
    "ProRes 422 (.mov)": (".mov", ["-c:v", "prores_ks", "-profile:v", "2", "-pix_fmt", "yuv422p10le"]),
}

#: Pads an odd width or height by one black pixel: both codecs need even sizes.
_EVEN_SIZE = "pad=ceil(iw/2)*2:ceil(ih/2)*2"

# The frame number in a file name: a run of hashes, {frame}, or %04d.
_PLACEHOLDER = re.compile(r"#+|\{frame\}|%0?(\d*)d")
# The last run of digits in a file name, for a path to one frame (Render's Output).
_LAST_DIGITS = re.compile(r"(\d+)(?!.*\d)")

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def find_frames(pattern: str) -> list[tuple[int, str]]:
    """The files on disk that match an image sequence pattern.

    Args:
        pattern: A path whose file name holds the frame number as ####, {frame} or %04d,
            or a path to one frame of the sequence (render.0010.png).

    Returns:
        (frame, path) pairs sorted by frame. Empty when nothing matches.
    """
    folder, name = os.path.split(pattern)
    match = None
    for match in _PLACEHOLDER.finditer(name):
        pass  # the last one is the frame number
    if match:
        text = match.group(0)
        width = len(text) if text.startswith("#") else int(match.group(1) or 0)
    elif digits := _LAST_DIGITS.search(name):
        match, width = digits, len(digits.group(1))
    else:
        return []
    prefix, suffix = name[:match.start()], name[match.end():]
    regex = re.compile(re.escape(prefix) + r"(\d+)" + re.escape(suffix) + r"\Z", re.IGNORECASE)
    try:
        names = os.listdir(folder or ".")
    except OSError:
        return []
    frames = []
    for candidate in names:
        found = regex.match(candidate)
        # zfill keeps render.0010.png and render.10.png apart, as the writer padded them.
        if found and found.group(1) == str(int(found.group(1))).zfill(width):
            frames.append((int(found.group(1)), os.path.join(folder, candidate)))
    return sorted(frames)


def _image2_input(frames: list[tuple[int, str]]) -> tuple[str, int] | None:
    """An ffmpeg image2 pattern and start number for frames, or None if it can't name them.

    Args:
        frames: (frame, path) pairs sorted by frame.

    Returns:
        (pattern, first frame), or None for a gap in the frames or names that differ.
    """
    first, path = frames[0]
    if [f for f, _ in frames] != list(range(first, first + len(frames))):
        return None
    folder, name = os.path.split(path)
    digits = re.search(r"(\d+)(?!.*\d)", name)
    prefix, suffix = name[:digits.start()], name[digits.end():]
    width = len(digits.group(1)) if digits.group(1).startswith("0") else 1
    pattern = os.path.join(folder, prefix.replace("%", "%%") + f"%0{width}d" + suffix.replace("%", "%%"))
    for frame, frame_path in frames:
        if frame_path != os.path.join(folder, f"{prefix}{str(frame).zfill(width)}{suffix}"):
            return None
    return pattern, first


def _link_frames(frames: list[tuple[int, str]], folder: str) -> str:
    """Link (or copy) frames into folder as 000000.ext, 000001.ext... for ffmpeg.

    Args:
        frames: (frame, path) pairs sorted by frame.
        folder: An empty folder to fill.

    Returns:
        The image2 pattern that names the linked files.
    """
    ext = os.path.splitext(frames[0][1])[1]
    for index, (_frame, path) in enumerate(frames):
        target = os.path.join(folder, f"{index:06d}{ext}")
        try:
            os.link(path, target)
        except OSError:  # another drive, or a file system without hard links
            shutil.copyfile(path, target)
    return os.path.join(folder, f"%06d{ext}")


def build_argv(ffmpeg: str, pattern: str, start: int, movie: str, fps: int, codec: str, quality: int) -> list[str]:
    """The ffmpeg command line that encodes an image2 sequence into movie.

    Args:
        ffmpeg: Path to the ffmpeg executable.
        pattern: The image2 input pattern (render.%04d.png).
        start: The first frame number in pattern.
        movie: The movie file to write.
        fps: Frames per second.
        codec: A key of _CODECS.
        quality: H.264 CRF, 0 (lossless) to 51 (worst).

    Returns:
        The argv list.
    """
    argv = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-framerate", str(fps), "-start_number", str(start), "-i", pattern,
            "-vf", _EVEN_SIZE, *_CODECS[codec][1]]
    if codec.startswith("H.264"):
        argv += ["-crf", str(quality)]
    return [*argv, "-r", str(fps), movie]


def _set_status(node, text: str) -> None:
    node.get_param("status").value = text


class _Encode:
    """One ffmpeg run: its command line, the movie it writes and the temp folder to delete."""

    def __init__(self, argv: list[str], movie: str, frame_count: int, scratch: str):
        self.argv = argv
        self.movie = movie
        self.frame_count = frame_count
        self.scratch = scratch


#: Frames to Movie chunks encoding in the background. One encode at a time per chunk.
_encoding: set[int] = set()


def _prepare(node) -> _Encode | None:
    """Find the frames and build the ffmpeg command, on the main thread.

    Args:
        node: The Frames to Movie chunk.

    Returns:
        The encode to run, or None when there is nothing to encode (the reason goes to Status).
    """
    stage = node.get_stage()
    pattern = chunk_utils.resolve_output_path(node.get_param("frames").value or "", stage, logger)
    movie = chunk_utils.resolve_output_path(node.get_param("output_movie").value or "", stage, logger)
    if not pattern or not movie:
        _set_status(node, "Set Frames and Output Movie first.")
        return None
    pattern, movie = chunk_utils.cwd_relative(pattern), chunk_utils.cwd_relative(movie)

    frames = find_frames(pattern)
    if not frames:
        _set_status(node, f"No frames found for {os.path.basename(pattern)}")
        logger.warning("Frames to Movie: no files match %s", pattern)
        return None

    codec = node.get_param("codec").value
    if codec not in _CODECS:
        codec = next(iter(_CODECS))
    ext = _CODECS[codec][0]
    if os.path.splitext(movie)[1].lower() != ext:
        movie = os.path.splitext(movie)[0] + ext  # the codec decides the container

    if imageio_ffmpeg is None:
        _set_status(node, "imageio-ffmpeg is not installed.")
        logger.error("Frames to Movie: pip install imageio-ffmpeg to make movies.")
        return None
    try:
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except RuntimeError as exc:
        _set_status(node, "ffmpeg not found.")
        logger.error("Frames to Movie: %s", exc)
        return None

    if parent := os.path.dirname(movie):
        os.makedirs(parent, exist_ok=True)

    fps = int(node.get_param("fps").value)
    quality = int(node.get_param("quality").value)
    scratch = tempfile.mkdtemp(prefix="frames_to_movie_")
    image2 = _image2_input(frames)
    if image2 is None:  # a gap or mixed names: give ffmpeg a clean, numbered copy
        image2 = (_link_frames(frames, scratch), 0)
    argv = build_argv(ffmpeg, image2[0], image2[1], movie, fps, codec, quality)
    return _Encode(argv, movie, len(frames), scratch)


def _encode(job: _Encode) -> subprocess.CompletedProcess | OSError:
    """Run ffmpeg. Touches no USD, so it is safe on a worker thread."""
    logger.info("Frames to Movie: %s", subprocess.list2cmdline(job.argv))
    try:
        return subprocess.run(job.argv, capture_output=True, text=True, errors="replace",
                              creationflags=_NO_WINDOW, check=False)
    except OSError as exc:
        return exc


def _finish(node, job: _Encode, result: subprocess.CompletedProcess | OSError) -> str:
    """Report an encode on the chunk, on the main thread.

    Args:
        node: The Frames to Movie chunk.
        job: The encode that ran.
        result: What ffmpeg returned, or the error that kept it from starting.

    Returns:
        The movie path written, or "" on failure (the reason goes to Status and the log).
    """
    shutil.rmtree(job.scratch, ignore_errors=True)
    if isinstance(result, OSError):
        _set_status(node, f"ffmpeg did not start: {result}")
        logger.error("Frames to Movie: ffmpeg did not start: %s", result)
        return ""
    if result.returncode != 0 or not os.path.isfile(job.movie):
        tail = "\n".join(result.stderr.strip().splitlines()[-15:])
        last = tail.splitlines()[-1] if tail else f"exit code {result.returncode}"
        _set_status(node, f"ffmpeg failed: {last}")
        logger.error("Frames to Movie: ffmpeg failed (exit code %s):\n%s", result.returncode, tail)
        return ""

    _set_status(node, f"{job.frame_count} frames -> {os.path.basename(job.movie)}")
    logger.info("Frames to Movie wrote %s (%d frames)", job.movie, job.frame_count)
    node.get_param("output").value = job.movie
    return job.movie


def make_movie(node) -> str:
    """Encode the chunk's Frames into its Output Movie and wait for it.

    Args:
        node: The Frames to Movie chunk.

    Returns:
        The movie path written, or "" on failure (the reason goes to Status and the log).
    """
    job = _prepare(node)
    if job is None:
        return ""
    return _finish(node, job, _encode(job))


def make_movie_in_background(node) -> None:
    """Start the encode on a worker thread and return at once; Status says when it is done.

    Args:
        node: The Frames to Movie chunk.
    """
    if id(node) in _encoding:
        logger.warning("Frames to Movie: already making this movie.")
        return
    job = _prepare(node)
    if job is None:
        return
    _encoding.add(id(node))
    _set_status(node, f"Encoding {job.frame_count} frames")
    scene = node.get_scene()

    def done(result):
        _encoding.discard(id(node))
        try:
            _finish(node, job, result)
        except Exception as exc:  # noqa: BLE001 - the chunk may have been deleted meanwhile
            logger.debug("Frames to Movie: could not report the finished movie (%s)", exc)

    def work():
        result = _encode(job)
        scene.call_later(lambda: done(result))

    threading.Thread(target=work, name="usdchunks-movie", daemon=True).start()


def _make_clicked(owner, notice, sender):
    make_movie_in_background(owner.get_chunk())


def main():
    labeled = chunk.ChunkFlags.ParamFlags.LABELED
    chunk_api = chunk.Chunk("Frames to Movie")
    chunk_api.add_params([
        chunk.LineEdit("frames", "Frames", default_value="render.####.png", flags=labeled,
                       tooltip="The image sequence, with #### for the frame number.\n"
                       "Wire a Render chunk's Output here; a path to one frame works too."),
        chunk.LineEdit("output_movie", "Output Movie", default_value="render.mp4", flags=labeled,
                       tooltip="The movie to write. Its folder is made if missing.\n"
                       "The extension follows the codec."),
        chunk.Slider("fps", "FPS", default_value=24, min_value=1, max_value=120, is_int=True),
        chunk.Menu("codec", "Codec", menu_items=list(_CODECS), default_value=next(iter(_CODECS))),
        chunk.Slider("quality", "Quality (CRF)", default_value=18, min_value=0, max_value=51, is_int=True,
                     tooltip="H.264 only. Lower is better and bigger: 0 is lossless, 18 looks clean, 28 is small."),
        chunk.Checkbox("make_on_commit", "Make Movie on Commit", default_value=True,
                       tooltip="Make the movie on every `usdchunks run`."),
    ])
    button = chunk.Button("make_movie", "Make Movie")
    button.add_callback(_make_clicked)
    chunk_api.add_params([
        button,
        chunk.Label("status", "not made yet", default_value="not made yet", tooltip="What the last Make Movie did."),
        chunk.LineEdit("output", "Output", flags=chunk.ChunkFlags.ParamFlags.OUTPUT | labeled),
    ])
    return chunk_api


def execute(node):
    """The cook authors nothing: the movie is made in commit() or by the button."""
    return


def commit(node):
    if node.get_param("make_on_commit").value:
        make_movie(node)


def config():
    return chunk.DefaultConfig(
        category=chunk.StandardCategory.USD_RENDER,
        icon="film",
        description="""
        Makes a movie (mp4, mov) from rendered frames, by hand or on every `usdchunks run`.

        Bakes an image sequence into one movie file with ffmpeg. Wire a Render chunk's
        Output into Frames (`@Render.output@`), or type the sequence path.

        The movie is written in commit() and by Make Movie, never while the chunk cooks,
        so `usdchunks run` renders first and then makes the movie. The chunk authors
        nothing on the stage.

        Parameters:
          - Frames: the image sequence. The frame number is #### (padded to the number
            of hashes), {frame} or %04d; a path to one frame (render.0010.png) also
            finds the rest. The frame range is whatever matching files exist. Gaps are
            fine: each file found is one frame of the movie.
          - Output Movie: the movie file. Its folder is made if missing, and the
            extension is set to match the codec.
          - FPS: frames per second.
          - Codec: H.264 (.mp4) to watch and share, or ProRes 422 (.mov) for editing.
            Odd image sizes get one black pixel of padding, since both need even sizes.
          - Quality (CRF): H.264 only; lower is better and bigger.
          - Make Movie on Commit: make the movie on every `usdchunks run`. On by default.
          - Make Movie: make it now, in the background, so you keep working while
            ffmpeg runs. Status says when it is done. Commit waits for it instead.
          - Status: how many frames went into which movie, or why it failed.
          - Output (output): the movie path written.

        ffmpeg comes from the imageio-ffmpeg package; its errors go to the log.
        """,
        requirements=["imageio-ffmpeg>=0.5,<1"],
    )
