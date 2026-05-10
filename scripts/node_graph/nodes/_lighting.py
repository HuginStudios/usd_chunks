"""Lighting nodes — rect, sphere, distant, dome, disk lights."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile

from pxr import Gf, Sdf, UsdGeom, UsdLux

from ._base import UsdNodeBase, PORT_COLORS

_log = logging.getLogger(__name__)

# ── Module-level helpers used by BackplateLightingNode ────────────────────────

_SUN_DEFAULTS: dict = {
    "sun_azimuth_deg": 0.0,
    "sun_elevation_deg": 90.0,
    "sun_color_r": 1.0,
    "sun_color_g": 1.0,
    "sun_color_b": 1.0,
    "sun_intensity": 1.0,
    "ambient_intensity": 0.3,
}


def _parse_sun_response(text: str) -> dict:
    """Extract sun parameters from Gemini's JSON response; return safe overcast-noon defaults on failure."""
    try:
        clean = re.sub(r"```[a-z]*\n?", "", text).strip().rstrip("`").strip()
        try:
            data = json.loads(clean)
        except (json.JSONDecodeError, ValueError):
            match = re.search(r"\{[^{}]*\}", clean, re.DOTALL)
            if not match:
                return dict(_SUN_DEFAULTS)
            data = json.loads(match.group())
        result = dict(_SUN_DEFAULTS)
        for k in _SUN_DEFAULTS:
            if k in data:
                result[k] = float(data[k])
        return result
    except Exception:
        return dict(_SUN_DEFAULTS)


def _azimuth_elevation_to_rotation(
    azimuth_deg: float, elevation_deg: float
) -> tuple[float, float, float]:
    """Convert sun azimuth/elevation to USD DistantLight rotateXYZ angles.

    USD DistantLight shines along -Z by default; elevation maps to X rotation,
    azimuth maps to Z rotation so the light faces the correct world direction.
    """
    return (-elevation_deg, 0.0, -azimuth_deg)


def _set_rotate_xyz(
    xformable: "UsdGeom.Xformable", rx: float, ry: float, rz: float
) -> None:
    """Set a rotateXYZ xformOp on *xformable*, reusing the existing op if present.

    USD raises an error if AddRotateXYZOp is called when the op already exists on
    the prim (e.g. on a second graph execution). We look up the existing op first
    and only add when it is missing.
    """
    existing = {op.GetOpName(): op for op in xformable.GetOrderedXformOps()}
    op = existing.get("xformOp:rotateXYZ") or xformable.AddRotateXYZOp()
    op.Set(Gf.Vec3f(rx, ry, rz))


class CreateRectLightNode(UsdNodeBase):
    NODE_NAME = "Create Rect Light"
    CATEGORY = "Lighting"

    def __init__(self) -> None:
        super().__init__()
        self.add_text_input("path", "Path", text="/RectLight")
        self.add_text_input("w", "Width", text="2.0")
        self.add_text_input("h", "Height", text="2.0")
        self.add_text_input("intensity", "Intensity", text="500")
        self.add_text_input("color_r", "Color R", text="1.0")
        self.add_text_input("color_g", "Color G", text="1.0")
        self.add_text_input("color_b", "Color B", text="1.0")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage) -> dict:
        p = self.get_property("path") or "/RectLight"
        light = UsdLux.RectLight.Define(stage, p)
        light.GetWidthAttr().Set(float(self.get_property("w") or 2))
        light.GetHeightAttr().Set(float(self.get_property("h") or 2))
        light.GetIntensityAttr().Set(float(self.get_property("intensity") or 500))
        r = float(self.get_property("color_r") or 1)
        g = float(self.get_property("color_g") or 1)
        b = float(self.get_property("color_b") or 1)
        light.GetColorAttr().Set(Gf.Vec3f(r, g, b))
        return {"prim": light.GetPrim()}


class CreateSphereLightNode(UsdNodeBase):
    NODE_NAME = "Create Sphere Light"
    CATEGORY = "Lighting"

    def __init__(self) -> None:
        super().__init__()
        self.add_text_input("path", "Path", text="/SphereLight")
        self.add_text_input("radius", "Radius", text="0.5")
        self.add_text_input("intensity", "Intensity", text="500")
        self.add_text_input("color_r", "Color R", text="1.0")
        self.add_text_input("color_g", "Color G", text="1.0")
        self.add_text_input("color_b", "Color B", text="1.0")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage) -> dict:
        p = self.get_property("path") or "/SphereLight"
        light = UsdLux.SphereLight.Define(stage, p)
        light.GetRadiusAttr().Set(float(self.get_property("radius") or 0.5))
        light.GetIntensityAttr().Set(float(self.get_property("intensity") or 500))
        r = float(self.get_property("color_r") or 1)
        g = float(self.get_property("color_g") or 1)
        b = float(self.get_property("color_b") or 1)
        light.GetColorAttr().Set(Gf.Vec3f(r, g, b))
        return {"prim": light.GetPrim()}


class CreateDistantLightNode(UsdNodeBase):
    NODE_NAME = "Create Distant Light"
    CATEGORY = "Lighting"

    def __init__(self) -> None:
        super().__init__()
        self.add_text_input("path", "Path", text="/DistantLight")
        self.add_text_input("intensity", "Intensity", text="500")
        self.add_text_input("angle", "Angle", text="0.53")
        self.add_text_input("color_r", "Color R", text="1.0")
        self.add_text_input("color_g", "Color G", text="1.0")
        self.add_text_input("color_b", "Color B", text="1.0")
        self.add_text_input("azimuth", "Azimuth (deg)", text="")
        self.add_text_input("elevation", "Elevation (deg)", text="")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage) -> dict:
        p = self.get_property("path") or "/DistantLight"
        light = UsdLux.DistantLight.Define(stage, p)
        light.GetIntensityAttr().Set(float(self._get_prop_value("intensity") or 500))
        light.GetAngleAttr().Set(float(self._get_prop_value("angle") or 0.53))
        r = float(self._get_prop_value("color_r") or 1)
        g = float(self._get_prop_value("color_g") or 1)
        b = float(self._get_prop_value("color_b") or 1)
        light.GetColorAttr().Set(Gf.Vec3f(r, g, b))
        az_str = str(self._get_prop_value("azimuth") or "").strip()
        el_str = str(self._get_prop_value("elevation") or "").strip()
        if az_str or el_str:
            az = float(az_str) if az_str else 0.0
            el = float(el_str) if el_str else 90.0
            rx, ry, rz = _azimuth_elevation_to_rotation(az, el)
            _set_rotate_xyz(UsdGeom.Xformable(light.GetPrim()), rx, ry, rz)
        return {"prim": light.GetPrim()}


class CreateDomeLightNode(UsdNodeBase):
    NODE_NAME = "Create Dome Light"
    CATEGORY = "Lighting"

    def __init__(self) -> None:
        super().__init__()
        self.add_text_input("path", "Path", text="/DomeLight")
        self.add_text_input("intensity", "Intensity", text="1.0")
        self.add_text_input("texture", "Texture File (HDRI)", text="")
        self.add_text_input("rotation_x", "Rotation X (deg)", text="")
        self.add_text_input("rotation_y", "Rotation Y (deg)", text="")
        self.add_text_input("rotation_z", "Rotation Z (deg)", text="")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage) -> dict:
        p = self.get_property("path") or "/DomeLight"
        light = UsdLux.DomeLight.Define(stage, p)
        light.GetIntensityAttr().Set(float(self.get_property("intensity") or 1))
        tex = str(self._get_prop_value("texture") or "").strip()
        if tex:
            light.GetTextureFileAttr().Set(Sdf.AssetPath(tex))
        rx_s = str(self._get_prop_value("rotation_x") or "").strip()
        ry_s = str(self._get_prop_value("rotation_y") or "").strip()
        rz_s = str(self._get_prop_value("rotation_z") or "").strip()
        if rx_s or ry_s or rz_s:
            _set_rotate_xyz(
                UsdGeom.Xformable(light.GetPrim()),
                float(rx_s) if rx_s else 0.0,
                float(ry_s) if ry_s else 0.0,
                float(rz_s) if rz_s else 0.0,
            )
        return {"prim": light.GetPrim()}


class CreateDiskLightNode(UsdNodeBase):
    NODE_NAME = "Create Disk Light"
    CATEGORY = "Lighting"

    def __init__(self) -> None:
        super().__init__()
        self.add_text_input("path", "Path", text="/DiskLight")
        self.add_text_input("radius", "Radius", text="1.0")
        self.add_text_input("intensity", "Intensity", text="500")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage) -> dict:
        p = self.get_property("path") or "/DiskLight"
        light = UsdLux.DiskLight.Define(stage, p)
        light.GetRadiusAttr().Set(float(self.get_property("radius") or 1))
        light.GetIntensityAttr().Set(float(self.get_property("intensity") or 500))
        return {"prim": light.GetPrim()}


class EstimateLightingNode(UsdNodeBase):
    """Calls Gemini to estimate blended sun direction, colour, and intensity from one or more reference photos.

    Connect up to four backplate images (image_0 … image_3). All connected images are sent
    to Gemini in a single call and it synthesises a unified lighting estimate — useful when
    you like the sky colour from one shot and the sun angle from another.

    Outputs individual string ports (azimuth, elevation, color_r/g/b, intensity, ambient).
    Wire these into CreateDistantLightNode auto-ports and/or GeneratePanoramaNode inputs.

    Auto-locks after a successful API call; press 'Estimate Lighting' to regenerate.
    """

    NODE_NAME = "Estimate Lighting"
    CATEGORY = "Lighting"
    _generate_button_label = "Estimate Lighting"

    _IMAGE_PORTS: tuple[str, ...] = ("image_0", "image_1", "image_2", "image_3")

    def __init__(self) -> None:
        super().__init__()
        for port in self._IMAGE_PORTS:
            self.add_input(port, color=PORT_COLORS["any"])
        self.add_output("azimuth", color=PORT_COLORS["string"])
        self.add_output("elevation", color=PORT_COLORS["string"])
        self.add_output("color_r", color=PORT_COLORS["string"])
        self.add_output("color_g", color=PORT_COLORS["string"])
        self.add_output("color_b", color=PORT_COLORS["string"])
        self.add_output("intensity", color=PORT_COLORS["string"])
        self.add_output("ambient", color=PORT_COLORS["string"])
        self.create_property("locked", True)
        self._cached_result: dict = {}

    def execute(self, stage) -> dict:
        """Analyse reference photos; return blended sun parameters as individual string outputs."""
        if self.get_property("locked"):
            return self._cached_result if self._cached_result else self._empty_result()

        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            _log.error("EstimateLightingNode: GEMINI_API_KEY not set")
            return self._empty_result()

        image_paths = [
            str(self._get_input_value(p) or "").strip()
            for p in self._IMAGE_PORTS
            if str(self._get_input_value(p) or "").strip()
        ]
        if not image_paths:
            return self._empty_result()

        sun = self._call_estimate_sun(api_key, image_paths)
        result = {
            "azimuth": str(sun["sun_azimuth_deg"]),
            "elevation": str(sun["sun_elevation_deg"]),
            "color_r": str(sun["sun_color_r"]),
            "color_g": str(sun["sun_color_g"]),
            "color_b": str(sun["sun_color_b"]),
            "intensity": str(sun["sun_intensity"]),
            "ambient": str(sun["ambient_intensity"]),
        }
        self._cached_result = result
        self.set_property("locked", True)
        return result

    def _empty_result(self) -> dict:
        return {
            k: ""
            for k in (
                "azimuth",
                "elevation",
                "color_r",
                "color_g",
                "color_b",
                "intensity",
                "ambient",
            )
        }

    def _open_as_png(self, image_path: str) -> "PILImage.Image":
        """Open any image and return as in-memory PNG to avoid Gemini JPEG rejection."""
        import io
        from PIL import Image as PILImage

        img = PILImage.open(image_path).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return PILImage.open(buf)

    def _call_estimate_sun(self, api_key: str, image_paths: list[str]) -> dict:
        """Send one or more reference images to Gemini and return a synthesised lighting estimate.

        When multiple images are provided Gemini is asked to blend the lighting across all
        references into a single coherent estimate rather than averaging numbers blindly.
        """
        try:
            from google import genai

            client = genai.Client(api_key=api_key)
            imgs = [self._open_as_png(p) for p in image_paths]

            n = len(imgs)
            if n == 1:
                ref_clause = "this photograph"
                blend_clause = ""
            else:
                ref_clause = f"these {n} reference photographs"
                blend_clause = (
                    " If the images show different lighting conditions, synthesise a single "
                    "combined estimate that best represents the shared or most visually dominant "
                    "lighting across all references — do not average numbers mechanically."
                )

            prompt = (
                f"Analyse {ref_clause} for lighting conditions.{blend_clause} "
                "Return ONLY valid JSON with exactly these keys, no other text:\n"
                "{\n"
                '  "sun_azimuth_deg": <float, 0=north clockwise to 360>,\n'
                '  "sun_elevation_deg": <float, 0=horizon 90=directly overhead>,\n'
                '  "sun_color_r": <float 0-1, red channel of direct sunlight>,\n'
                '  "sun_color_g": <float 0-1, green channel of direct sunlight>,\n'
                '  "sun_color_b": <float 0-1, blue channel of direct sunlight>,\n'
                '  "sun_intensity": <float, 1=overcast diffuse 5=harsh direct sun>,\n'
                '  "ambient_intensity": <float 0-1, overall sky/ambient brightness>\n'
                "}\n"
                "Output nothing except the JSON object."
            )
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[prompt, *imgs],
            )
            text = next((p.text for p in response.parts if p.text), "")
            return _parse_sun_response(text)
        except ImportError:
            _log.warning("EstimateLightingNode: pip install google-genai pillow")
            return dict(_SUN_DEFAULTS)
        except Exception as e:
            err = str(e)
            if "429" in err or "RESOURCE_EXHAUSTED" in err:
                _log.error("EstimateLightingNode: Gemini quota exceeded")
            elif "INVALID_ARGUMENT" in err:
                _log.error(
                    "EstimateLightingNode: Unsupported image format in: %s", image_paths
                )
            else:
                _log.error("EstimateLightingNode: Sun estimation failed: %s", err)
            return dict(_SUN_DEFAULTS)


class GeneratePanoramaNode(UsdNodeBase):
    """Calls Imagen 4 to generate a 360° equirectangular panorama from estimated sun parameters.

    Wire EstimateLightingNode outputs (azimuth, elevation, color_r/g/b, intensity, ambient)
    into the matching input ports, or leave unconnected to use sunny-noon defaults.
    The image_path string output wires directly to CreateDomeLightNode._texture.

    panorama_mode controls output aspect ratio:
      pad_2_1  — pad 16:9 Imagen output to 2:1 with black bands (correct lat-long mapping)
      full_360 — pass the 16:9 image through unchanged

    Auto-locks after successful generation; press 'Generate Panorama' to regenerate.
    """

    NODE_NAME = "Generate Panorama"
    CATEGORY = "Lighting"
    _generate_button_label = "Generate Panorama"

    def __init__(self) -> None:
        super().__init__()
        self.add_input("azimuth", color=PORT_COLORS["string"])
        self.add_input("elevation", color=PORT_COLORS["string"])
        self.add_input("color_r", color=PORT_COLORS["string"])
        self.add_input("color_g", color=PORT_COLORS["string"])
        self.add_input("color_b", color=PORT_COLORS["string"])
        self.add_input("intensity", color=PORT_COLORS["string"])
        self.add_input("ambient", color=PORT_COLORS["string"])
        self.add_combo_menu(
            "panorama_mode",
            "Panorama Mode",
            items=["pad_2_1", "full_360"],
        )
        self.add_output("image_path", color=PORT_COLORS["string"])
        self.create_property("locked", True)
        self._cached_result: dict = {}

    _SUN_PARAM_PORTS = (
        "azimuth",
        "elevation",
        "color_r",
        "color_g",
        "color_b",
        "intensity",
        "ambient",
    )

    def _upstream_ready(self) -> bool:
        """Return False if any connected sun-param port has no value yet.

        An empty value on a connected port means the upstream EstimateLightingNode
        has not been executed (or was locked without a result), so calling Imagen
        would waste tokens generating a generic default sky.
        Unconnected ports are fine — they use _SUN_DEFAULTS intentionally.
        """
        for name in self._SUN_PARAM_PORTS:
            port = self.get_input(name)
            if port and port.connected_ports():
                val = self._get_input_value(name)
                if val is None or str(val).strip() == "":
                    return False
        return True

    def execute(self, stage) -> dict:
        """Generate panorama from wired sun params and return image_path string output."""
        if self.get_property("locked"):
            return self._cached_result if self._cached_result else {"image_path": ""}

        if not self._upstream_ready():
            _log.warning(
                "GeneratePanoramaNode: upstream has not produced sun params yet — "
                "run EstimateLightingNode first to avoid wasting API tokens"
            )
            return {"image_path": ""}

        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            _log.error("GeneratePanoramaNode: GEMINI_API_KEY not set")
            return {"image_path": ""}

        sun_params = self._gather_sun_params()
        path = self._generate_panorama(api_key, sun_params)
        result = {"image_path": path or ""}
        if path:
            self._cached_result = result
            self.set_property("locked", True)
        return result

    def _gather_sun_params(self) -> dict:
        """Read sun params from connected input ports, falling back to defaults."""

        def _flt(name: str, default: float) -> float:
            v = self._get_input_value(name)
            try:
                return float(v) if v not in (None, "") else default
            except (ValueError, TypeError):
                return default

        return {
            "sun_azimuth_deg": _flt("azimuth", _SUN_DEFAULTS["sun_azimuth_deg"]),
            "sun_elevation_deg": _flt("elevation", _SUN_DEFAULTS["sun_elevation_deg"]),
            "sun_color_r": _flt("color_r", _SUN_DEFAULTS["sun_color_r"]),
            "sun_color_g": _flt("color_g", _SUN_DEFAULTS["sun_color_g"]),
            "sun_color_b": _flt("color_b", _SUN_DEFAULTS["sun_color_b"]),
            "sun_intensity": _flt("intensity", _SUN_DEFAULTS["sun_intensity"]),
            "ambient_intensity": _flt("ambient", _SUN_DEFAULTS["ambient_intensity"]),
        }

    def _build_panorama_prompt(self, sun_params: dict) -> str:
        """Translate structured sun parameters into a descriptive text prompt for Imagen."""
        elevation = sun_params["sun_elevation_deg"]
        intensity = sun_params["sun_intensity"]
        r, g = sun_params["sun_color_r"], sun_params["sun_color_g"]
        b = sun_params["sun_color_b"]
        ambient = sun_params["ambient_intensity"]

        if elevation > 60:
            time_desc = "midday sun directly overhead"
        elif elevation > 30:
            time_desc = "afternoon sun at medium height"
        elif elevation > 10:
            time_desc = "golden hour sun low on the horizon"
        else:
            time_desc = "overcast sky near dawn or dusk"

        if intensity > 4:
            weather_desc = "harsh direct sunlight, deep blue clear sky"
        elif intensity > 2:
            weather_desc = "bright partly cloudy sky, soft shadows"
        else:
            weather_desc = "overcast diffuse lighting, flat uniform sky"

        if r > g and r > b + 0.1:
            warmth_desc = "warm golden-orange sunlight"
        elif b > r + 0.1:
            warmth_desc = "cool bluish daylight"
        else:
            warmth_desc = "neutral white daylight"

        return (
            f"A seamless 360-degree equirectangular panorama environment map with a 2:1 aspect ratio, "
            f"suitable as a dome light texture for 3D rendering. "
            f"{time_desc.capitalize()}, {weather_desc}. {warmth_desc.capitalize()}. "
            f"Ambient sky brightness {ambient:.1f} on a 0-1 scale. "
            f"Pure sky and horizon, no foreground objects, no people, no text. "
            f"Photorealistic, seamlessly tileable at the left and right edges."
        )

    def _generate_panorama(self, api_key: str, sun_params: dict) -> str | None:
        """Call Imagen 4 to generate a panorama; returns absolute path to the saved PNG."""
        try:
            from google import genai

            client = genai.Client(api_key=api_key)
            prompt = self._build_panorama_prompt(sun_params)

            response = client.models.generate_images(
                model="imagen-4.0-ultra-generate-001",
                prompt=prompt,
                config=genai.types.GenerateImagesConfig(
                    number_of_images=1,
                    aspect_ratio="16:9",
                ),
            )

            import time

            out_path = os.path.join(
                tempfile.gettempdir(), f"backplate_panorama_{int(time.time())}.png"
            )
            if response.generated_images:
                response.generated_images[0].image.save(out_path)
                self._apply_panorama_mode(out_path)
                _log.info("GeneratePanoramaNode: panorama saved to %s", out_path)
                return out_path

            _log.warning("GeneratePanoramaNode: Imagen returned no image")
            return None
        except ImportError:
            _log.warning("GeneratePanoramaNode: pip install google-genai pillow")
            return None
        except Exception as e:
            err = str(e)
            if "429" in err or "RESOURCE_EXHAUSTED" in err:
                _log.error(
                    "GeneratePanoramaNode: Imagen quota exceeded — "
                    "billing required at aistudio.google.com → Settings → Billing"
                )
            else:
                _log.error("GeneratePanoramaNode: Panorama generation failed: %s", err)
            return None

    def _apply_panorama_mode(self, out_path: str) -> None:
        """Pad the Imagen 16:9 output to 2:1 equirectangular if panorama_mode is 'pad_2_1'.

        16:9 is taller than 2:1 for the same width, so we extend the WIDTH with black
        side bands to reach width = 2 × height. This represents the missing longitude
        coverage in the equirectangular projection without cropping any content.
        'full_360' passes the image through unchanged.
        """
        mode = self.get_property("panorama_mode") or "pad_2_1"
        if mode != "pad_2_1":
            return
        try:
            from PIL import Image as PILImage

            img = PILImage.open(out_path)
            w, h = img.size
            target_w = h * 2  # 2:1 means width = 2 × height
            if w >= target_w:
                return  # already at or wider than 2:1
            padded = PILImage.new("RGB", (target_w, h), (0, 0, 0))
            left_offset = (target_w - w) // 2
            padded.paste(img, (left_offset, 0))
            padded.save(out_path)
            _log.info(
                "GeneratePanoramaNode: padded panorama from %dx%d to %dx%d",
                w,
                h,
                target_w,
                h,
            )
        except Exception as e:
            _log.warning("GeneratePanoramaNode: panorama padding failed: %s", e)
