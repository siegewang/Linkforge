import os
import base64
import uuid
import zipfile
import io
import logging
from pathlib import Path
from typing import Optional
from PIL import Image, ImageDraw
from config import Config

logger = logging.getLogger(__name__)

def get_thumbnails_dir() -> Path:
    d = Path(getattr(Config, "THUMBNAILS_DIR", os.path.join(os.path.dirname(os.path.abspath(Config.DB_PATH)), "thumbnails")))
    d.mkdir(parents=True, exist_ok=True)
    return d

def extract_embedded_3mf_thumbnail(file_path: Path, model_id: int) -> Optional[str]:
    """
    Extracts high-resolution embedded preview / plate thumbnail image directly from a 3MF file
    (Bambu Studio, OrcaSlicer, PrusaSlicer) and saves as WebP.
    """
    file_path = Path(file_path)
    try:
        with zipfile.ZipFile(file_path, "r") as z:
            names = z.namelist()

            # Priority candidates for Bambu Studio & OrcaSlicer
            candidates = [
                "Metadata/plate_1.png",
                "Metadata/plate_1_small.png",
                "Auxiliaries/.thumbnails/thumbnail_middle.png",
                "Auxiliaries/.thumbnails/thumbnail_3mf.png",
                "Auxiliaries/.thumbnails/thumbnail_small.png",
                "Metadata/top_1.png",
                "Metadata/pick_1.png",
                "Metadata/thumbnail.png"
            ]

            match = next((c for c in candidates if c in names), None)
            if not match:
                # Dynamic search for any plate, thumbnail, or model picture
                valid_exts = (".png", ".webp", ".jpg", ".jpeg")
                match = next((
                    n for n in names
                    if any(n.lower().endswith(ext) for ext in valid_exts)
                    and any(k in n.lower() for k in ["plate", "thumb", "top", "model pictures", "picture"])
                ), None)

            if match:
                img_data = z.read(match)
                image = Image.open(io.BytesIO(img_data))

                if image.mode in ("RGBA", "P"):
                    # Create clean slate background
                    background = Image.new("RGBA", image.size, (15, 23, 42, 255))
                    background.paste(image, (0, 0), image)
                    image = background.convert("RGB")
                else:
                    image = image.convert("RGB")

                # Optimize & resize to standard thumbnail dimensions (up to 512x512)
                image.thumbnail((512, 512), Image.Resampling.LANCZOS)

                filename = f"thumb_{model_id}_embedded.webp"
                output_path = get_thumbnails_dir() / filename
                image.save(output_path, "WEBP", quality=88)
                logger.info(f"Extracted embedded 3MF thumbnail '{match}' for model {model_id} -> {filename}")
                return filename

    except Exception as e:
        logger.warning(f"Could not extract embedded 3MF thumbnail for {file_path}: {e}")

    return None

def save_thumbnail_from_data_url(data_url: str, model_id: int) -> Optional[str]:
    """
    Saves a data URL (e.g. data:image/webp;base64,...) as a WebP thumbnail file.
    """
    try:
        if "," in data_url:
            header, encoded = data_url.split(",", 1)
        else:
            encoded = data_url

        image_bytes = base64.b64decode(encoded)
        image = Image.open(io.BytesIO(image_bytes))
        
        # Ensure RGB mode for WebP saving
        if image.mode in ("RGBA", "P"):
            background = Image.new("RGBA", image.size, (15, 23, 42, 255))
            background.paste(image, (0, 0), image)
            image = background.convert("RGB")
        else:
            image = image.convert("RGB")

        # Resize to standard thumbnail size maintaining aspect ratio
        image.thumbnail((512, 512), Image.Resampling.LANCZOS)
        
        filename = f"thumb_{model_id}_{uuid.uuid4().hex[:8]}.webp"
        output_path = get_thumbnails_dir() / filename
        image.save(output_path, "WEBP", quality=85)
        
        return filename
    except Exception as e:
        logger.error(f"Error saving thumbnail: {e}")
        return None

def generate_placeholder_thumbnail(model_id: int, name: str, file_format: str) -> str:
    """
    Generates a stylish dark-mode placeholder thumbnail for a 3D model.
    """
    filename = f"thumb_{model_id}_placeholder.webp"
    output_path = get_thumbnails_dir() / filename
    if output_path.exists():
        return filename

    try:
        width, height = 480, 320
        image = Image.new("RGB", (width, height), (15, 23, 42))  # Slate 900
        draw = ImageDraw.Draw(image)

        # Draw grid lines for 3D bed feel
        grid_color = (30, 41, 59)
        for x in range(0, width, 40):
            draw.line([(x, 0), (x, height)], fill=grid_color, width=1)
        for y in range(0, height, 40):
            draw.line([(0, y), (width, y)], fill=grid_color, width=1)

        # Draw isometric wireframe box in center
        cx, cy = width // 2, height // 2 - 10
        accent = (56, 189, 248)  # Sky blue
        accent_dark = (14, 116, 144)
        
        pts_top = [(cx, cy - 50), (cx + 60, cy - 20), (cx, cy + 10), (cx - 60, cy - 20)]
        draw.polygon(pts_top, outline=accent, fill=(24, 49, 83))
        
        draw.line([(cx - 60, cy - 20), (cx - 60, cy + 40)], fill=accent, width=2)
        draw.line([(cx + 60, cy - 20), (cx + 60, cy + 40)], fill=accent, width=2)
        draw.line([(cx, cy + 10), (cx, cy + 70)], fill=accent, width=2)
        draw.line([(cx - 60, cy + 40), (cx, cy + 70)], fill=accent_dark, width=2)
        draw.line([(cx + 60, cy + 40), (cx, cy + 70)], fill=accent_dark, width=2)

        # Format label pill
        fmt_text = file_format.upper().replace(".", "")
        draw.rectangle([(cx - 45, cy + 85), (cx + 45, cy + 110)], fill=(30, 41, 59), outline=accent, width=1)
        draw.text((cx, cy + 97), fmt_text, fill=(255, 255, 255), anchor="mm")

        image.save(output_path, "WEBP", quality=80)
        return filename
    except Exception:
        return ""
