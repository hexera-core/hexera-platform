# Responsibility: Turn a rendered scene into the evidence a reviewer is shown, and a session into a video.
# Boundaries: it owns the save directory and the shot sequence; it decides nothing about what to render.
from __future__ import annotations

import base64
import io
import logging
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import pyvista as pv
from PIL import Image

logger = logging.getLogger(__name__)

#: ffmpeg is invoked with an explicit argument vector, never a shell string.
VIDEO_NAME = "review_session.mp4"
VIDEO_TIMEOUT_S = 60


def screenshot_array(plotter: pv.Plotter, **kwargs: Any) -> np.ndarray:
    rw = plotter.render_window
    if rw is None:
        raise RuntimeError("pyvista plotter has no render window (already closed?)")
    rw.Render()
    shot = plotter.screenshot(return_img=True, **kwargs)
    if shot is None:
        raise RuntimeError("pyvista returned no image for an off-screen screenshot")
    img: np.ndarray = np.asarray(shot)
    if img.ndim == 3 and img.shape[2] == 4:
        img = img[:, :, :3]
    return img


class SessionCapture:

    def __init__(self, save_dir: Path | None) -> None:
        self._save_dir = save_dir
        self._shot_counter = 0
        self._last_shot_path: Path | None = None
        if self._save_dir is not None:
            # Created here rather than by the caller: the directory exists for frames, and the
            # authority that writes them is the one that has to be sure it is there. A directory
            # that cannot be created is a save failure like any other - see the module note.
            try:
                self._save_dir.mkdir(parents=True, exist_ok=True)
            except Exception as exc:  # noqa: BLE001 - saving is a convenience, never the review
                logger.warning("render backend: could not create save directory: %s", exc)

    @property
    def last_shot_path(self) -> Path | None:
        return self._last_shot_path

    @property
    def shot_count(self) -> int:
        return self._shot_counter

    def png_bytes(self, plotter: Any) -> bytes:
        plotter.camera_set = True
        img_arr = screenshot_array(plotter)
        buf = io.BytesIO()
        Image.fromarray(img_arr.astype(np.uint8)).save(buf, format="PNG")
        return buf.getvalue()

    def save_and_encode(self, data: bytes) -> str:
        self._shot_counter += 1
        self._last_shot_path = None
        if self._save_dir is not None:
            shot_path = self._save_dir / f"{self._shot_counter:03d}.png"
            try:
                shot_path.write_bytes(data)
                self._last_shot_path = shot_path
            except Exception as exc:  # noqa: BLE001 - saving is a convenience, never the review
                logger.warning("render backend: could not save screenshot: %s", exc)
        return base64.b64encode(data).decode()

    def screenshot(self, plotter: Any) -> str:
        return self.save_and_encode(self.png_bytes(plotter))

    def assemble_video(self) -> None:
        if self._save_dir is None or self._shot_counter == 0:
            return
        video_path = self._save_dir / VIDEO_NAME
        cmd = [
            "ffmpeg", "-y",
            "-framerate", "0.5",
            "-i", str(self._save_dir / "%03d.png"),
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-vcodec", "libx264", "-preset", "fast", "-crf", "23",
            str(video_path),
        ]
        try:
            subprocess.run(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                timeout=VIDEO_TIMEOUT_S, check=True,
            )
            logger.info("render backend: video assembled - %d frames → %s",
                        self._shot_counter, video_path)
        except subprocess.CalledProcessError as exc:
            logger.warning("render backend: ffmpeg failed (rc=%d): %s",
                           exc.returncode, exc.stderr.decode(errors="replace")[:500])
        except Exception as exc:  # noqa: BLE001 - the frames are the evidence, not the clip
            logger.warning("render backend: could not assemble video: %s", exc)


__all__ = ["VIDEO_NAME", "VIDEO_TIMEOUT_S", "SessionCapture", "screenshot_array"]
