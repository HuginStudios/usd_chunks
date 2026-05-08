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
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage) -> dict:
        p = self.get_property("path") or "/DistantLight"
        light = UsdLux.DistantLight.Define(stage, p)
        light.GetIntensityAttr().Set(float(self.get_property("intensity") or 500))
        light.GetAngleAttr().Set(float(self.get_property("angle") or 0.53))
        r = float(self.get_property("color_r") or 1)
        g = float(self.get_property("color_g") or 1)
        b = float(self.get_property("color_b") or 1)
        light.GetColorAttr().Set(Gf.Vec3f(r, g, b))
        return {"prim": light.GetPrim()}


class CreateDomeLightNode(UsdNodeBase):
    NODE_NAME = "Create Dome Light"
    CATEGORY = "Lighting"

    def __init__(self) -> None:
        super().__init__()
        self.add_text_input("path", "Path", text="/DomeLight")
        self.add_text_input("intensity", "Intensity", text="1.0")
        self.add_text_input("texture", "Texture File (HDRI)", text="")
        self.add_output("prim", color=PORT_COLORS["prim"])

    def execute(self, stage) -> dict:
        p = self.get_property("path") or "/DomeLight"
        light = UsdLux.DomeLight.Define(stage, p)
        light.GetIntensityAttr().Set(float(self.get_property("intensity") or 1))
        tex = (self.get_property("texture") or "").strip()
        if tex:
            light.GetTextureFileAttr().Set(Sdf.AssetPath(tex))
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


class BackplateLightingNode(UsdNodeBase):
    """Sets up DomeLight + DistantLight to match a backplate photo using two Gemini calls.

    Call 1 (gemini-2.5-flash, text): estimates sun azimuth, elevation, colour, intensity as JSON.
    Call 2 (gemini-2.5-flash-image): generates a 360° lat-long equirectangular panorama for the DomeLight texture.
    """

    NODE_NAME = "Backplate Lighting"
    CATEGORY = "Lighting"

    def __init__(self) -> None:
        super().__init__()
        self.add_input("image", color=PORT_COLORS["any"])
        self.add_text_input("dome_path", "Dome Path", text="/World/Lights/BackplateDome")
        self.add_text_input("sun_path", "Sun Path", text="/World/Lights/BackplateSun")
        self.add_text_input("dome_intensity", "Dome Intensity", text="1.0")
        self.add_text_input("sun_intensity_scale", "Sun Intensity Scale", text="1.0")
        self.add_output("dome_light", color=PORT_COLORS["prim"])
        self.add_output("sun_light", color=PORT_COLORS["prim"])

    def execute(self, stage) -> dict:
        """Run both Gemini calls then create DomeLight + DistantLight on the stage."""
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            self._exec_error_msg = (
                "GEMINI_API_KEY is not set — export GEMINI_API_KEY=<your key>"
            )
            return {"dome_light": None, "sun_light": None}

        image_path = str(self._get_input_value("image") or "").strip()
        if not image_path:
            return {"dome_light": None, "sun_light": None}

        sun_params = self._estimate_sun(api_key, image_path)
        panorama_path = self._generate_panorama(api_key, image_path)

        dome_prim = self._create_dome_light(stage, panorama_path)
        sun_prim = self._create_sun_light(stage, sun_params)

        return {"dome_light": dome_prim, "sun_light": sun_prim}

    def _estimate_sun(self, api_key: str, image_path: str) -> dict:
        """Call Gemini (text model) to estimate sun direction, colour, and intensity from the backplate."""
        try:
            from google import genai
            from PIL import Image as PILImage

            client = genai.Client(api_key=api_key)
            img = PILImage.open(image_path)

            prompt = (
                "Analyse this photograph for lighting conditions. "
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
                contents=[prompt, img],
            )
            text = next((p.text for p in response.parts if p.text), "")
            return _parse_sun_response(text)
        except ImportError:
            _log.warning("BackplateLightingNode: pip install google-genai pillow")
            return dict(_SUN_DEFAULTS)
        except Exception as e:
            err = str(e)
            if "429" in err or "RESOURCE_EXHAUSTED" in err:
                _log.error(
                    "BackplateLightingNode: Gemini quota exceeded during sun estimation"
                )
            elif "INVALID_ARGUMENT" in err:
                _log.error(
                    "BackplateLightingNode: Unsupported image format for sun estimation: %s",
                    image_path,
                )
            else:
                _log.error("BackplateLightingNode: Sun estimation failed: %s", err)
            return dict(_SUN_DEFAULTS)

    def _generate_panorama(self, api_key: str, image_path: str) -> str | None:
        """Call Gemini (image model) to generate a 360° equirectangular panorama matching the backplate lighting."""
        try:
            from google import genai
            from PIL import Image as PILImage

            client = genai.Client(api_key=api_key)
            img = PILImage.open(image_path)

            prompt = (
                "Generate a 360-degree equirectangular lat-long panorama environment map "
                "that exactly matches the lighting conditions in this photograph. "
                "Requirements: 2:1 aspect ratio equirectangular projection suitable for use "
                "as a dome light HDRI texture; the sun/sky direction, colour temperature, and "
                "intensity must match the input photo; seamlessly tileable at the left-right edges. "
                "Output ONLY the image, no text or explanation."
            )
            response = client.models.generate_content(
                model="gemini-2.5-flash-image",
                contents=[prompt, img],
            )
            out_path = os.path.join(tempfile.gettempdir(), "backplate_panorama.png")
            for part in response.parts:
                if part.inline_data is not None:
                    part.as_image().save(out_path)
                    return out_path
            _log.warning(
                "BackplateLightingNode: Gemini returned no image for panorama generation"
            )
            return None
        except ImportError:
            _log.warning("BackplateLightingNode: pip install google-genai pillow")
            return None
        except Exception as e:
            err = str(e)
            if "429" in err or "RESOURCE_EXHAUSTED" in err:
                _log.error(
                    "BackplateLightingNode: Gemini quota exceeded during panorama generation"
                    " — image generation requires billing at aistudio.google.com → Settings → Billing"
                )
            elif "INVALID_ARGUMENT" in err:
                _log.error(
                    "BackplateLightingNode: Unsupported image format for panorama generation: %s",
                    image_path,
                )
            else:
                _log.error(
                    "BackplateLightingNode: Panorama generation failed: %s", err
                )
            return None

    def _create_dome_light(self, stage, panorama_path: str | None) -> object:
        """Create DomeLight on the stage, applying the generated panorama as texture if available."""
        try:
            path = self.get_property("dome_path") or "/World/Lights/BackplateDome"
            intensity = float(self.get_property("dome_intensity") or 1.0)
            light = UsdLux.DomeLight.Define(stage, path)
            light.GetIntensityAttr().Set(intensity)
            if panorama_path:
                light.GetTextureFileAttr().Set(Sdf.AssetPath(panorama_path))
            return light.GetPrim()
        except Exception as e:
            _log.error("BackplateLightingNode: Failed to create DomeLight: %s", e)
            return None

    def _create_sun_light(self, stage, sun_params: dict) -> object:
        """Create DistantLight on the stage with rotation and colour derived from sun_params."""
        try:
            path = self.get_property("sun_path") or "/World/Lights/BackplateSun"
            scale = float(self.get_property("sun_intensity_scale") or 1.0)
            light = UsdLux.DistantLight.Define(stage, path)
            light.GetIntensityAttr().Set(sun_params["sun_intensity"] * scale)
            light.GetColorAttr().Set(
                Gf.Vec3f(
                    sun_params["sun_color_r"],
                    sun_params["sun_color_g"],
                    sun_params["sun_color_b"],
                )
            )
            rx, ry, rz = _azimuth_elevation_to_rotation(
                sun_params["sun_azimuth_deg"], sun_params["sun_elevation_deg"]
            )
            xformable = UsdGeom.Xformable(light.GetPrim())
            xformable.AddRotateXYZOp().Set(Gf.Vec3f(rx, ry, rz))
            return light.GetPrim()
        except Exception as e:
            _log.error("BackplateLightingNode: Failed to create DistantLight: %s", e)
            return None
