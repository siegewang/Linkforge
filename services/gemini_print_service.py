import json
import re
import logging
import sqlite3
import os
from pathlib import Path
from typing import Dict, Any, Optional, List
import requests
from config import Config

logger = logging.getLogger(__name__)

FALLBACK_PROMPT = """You are an expert 3D printing engineer. Analyze the 3D model asset information provided and generate optimal metadata and slicer recommendations for printing on a modern FDM printer like the Bambu Lab P2S with AMS 2 Pro.

Return ONLY a valid JSON object matching this schema:
{
  "name": "Clean Human-Readable Title",
  "category": "Tools & Utility | Desk & Organization | Home & Living | Toys & Figures | Electronics & Gadgets | Mechanical | Art & Decor",
  "tags": ["tag1", "tag2", "tag3", "tag4"],
  "description": "2-3 concise, informative sentences about the model, its purpose, and print considerations.",
  "material_recommendation": "PLA | PETG | TPU | ABS | ASA",
  "slicer_settings": {
    "layer_height": "0.16mm | 0.20mm | 0.28mm",
    "infill_percentage": 15,
    "infill_pattern": "Gyroid | Grid | Honeycomb",
    "wall_loops": 3,
    "supports": "None | Tree (auto) | Normal (manual)",
    "bed_temp_c": 55,
    "nozzle_temp_c": 215,
    "brim": "None | Auto | Outer brim only",
    "orientation_advice": "Recommended print orientation"
  }
}
"""

def _get_api_key() -> str:
    try:
        conn = sqlite3.connect(Config.DB_PATH, timeout=5)
        for key in ["gemini_api_key", "ai_api_key"]:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            if row and row[0] and row[0].strip():
                conn.close()
                return row[0].strip()
        conn.close()
    except Exception:
        pass
    return os.environ.get("GEMINI_API_KEY", "").strip() or os.environ.get("AI_API_KEY", "").strip()

def _get_gemini_model() -> str:
    try:
        conn = sqlite3.connect(Config.DB_PATH, timeout=5)
        row = conn.execute("SELECT value FROM settings WHERE key = 'gemini_model'").fetchone()
        conn.close()
        if row and row[0] and row[0].strip():
            return row[0].strip()
    except Exception:
        pass
    return os.environ.get("GEMINI_MODEL", "gemini-1.5-flash").strip()

def analyze_model_with_gemini(
    filename: str,
    file_format: str,
    geometry: Dict[str, Any],
    custom_instructions: Optional[str] = None
) -> Dict[str, Any]:
    """
    Analyzes model file metadata and geometry using Gemini 1.5 Flash API.
    Falls back gracefully if API key is not present or API call fails.
    """
    api_key = _get_api_key()
    if api_key:
        try:
            model_name = _get_gemini_model()
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
            
            prompt_input = (
                f"Model Filename: {filename}\n"
                f"File Format: {file_format}\n"
                f"Bounding Box: X={geometry.get('bbox_x_mm', 0)}mm, Y={geometry.get('bbox_y_mm', 0)}mm, Z={geometry.get('bbox_z_mm', 0)}mm\n"
                f"Estimated Volume: {geometry.get('volume_cm3', 0)} cm³\n"
                f"Triangles: {geometry.get('triangles', 0)}\n"
            )
            if custom_instructions:
                prompt_input += f"User Instructions: {custom_instructions}\n"

            payload = {
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {"text": FALLBACK_PROMPT},
                            {"text": prompt_input}
                        ]
                    }
                ],
                "generationConfig": {
                    "temperature": 0.2,
                    "responseMimeType": "application/json"
                }
            }

            res = requests.post(url, json=payload, timeout=25)
            if res.status_code == 200:
                data = res.json()
                candidates = data.get("candidates", [])
                if candidates and "content" in candidates[0]:
                    parts = candidates[0]["content"].get("parts", [])
                    if parts and "text" in parts[0]:
                        text_content = parts[0]["text"]
                        parsed = json.loads(text_content)
                        return _validate_and_sanitize_gemini_output(parsed, filename)
            else:
                logger.warning(f"Gemini API returned status {res.status_code}: {res.text}")
        except Exception as e:
            logger.error(f"Error calling Gemini API: {e}", exc_info=True)

    # Fallback to local heuristic rule-based engine
    return _generate_heuristic_metadata(filename, file_format, geometry)


def _validate_and_sanitize_gemini_output(data: Dict[str, Any], filename: str) -> Dict[str, Any]:
    name = data.get("name") or _clean_filename_to_title(filename)
    category = data.get("category") or "General"
    tags = data.get("tags") or ["3dprint", "model"]
    if not isinstance(tags, list):
        tags = [str(tags)]
    tags = [t.lower().strip() for t in tags if t]

    desc = data.get("description") or f"3D printable model parsed from {filename}."
    mat = data.get("material_recommendation") or "PLA"
    slicer = data.get("slicer_settings") or {}
    
    clean_slicer = {
        "layer_height": slicer.get("layer_height", "0.20mm"),
        "infill_percentage": int(slicer.get("infill_percentage", 15)),
        "infill_pattern": slicer.get("infill_pattern", "Gyroid"),
        "wall_loops": int(slicer.get("wall_loops", 3)),
        "supports": slicer.get("supports", "None"),
        "bed_temp_c": int(slicer.get("bed_temp_c", 60)),
        "nozzle_temp_c": int(slicer.get("nozzle_temp_c", 215)),
        "brim": slicer.get("brim", "Auto"),
        "orientation_advice": slicer.get("orientation_advice", "Place flattest face on the build plate.")
    }

    return {
        "name": name,
        "category": category,
        "tags": tags,
        "description": desc,
        "material_recommendation": mat,
        "slicer_settings": clean_slicer
    }


def _clean_filename_to_title(filename: str) -> str:
    base = Path(filename).stem
    title = re.sub(r"[_\-\.]+", " ", base)
    title = re.sub(r"\b(v\d+|rev\d+|final)\b", "", title, flags=re.IGNORECASE)
    title = " ".join(word.capitalize() for word in title.split())
    return title.strip() or filename


def _generate_heuristic_metadata(filename: str, file_format: str, geometry: Dict[str, Any]) -> Dict[str, Any]:
    """Smart fallback generator based on filename keywords and geometry."""
    lower_fn = filename.lower()
    title = _clean_filename_to_title(filename)

    category = "General"
    mat = "PLA"
    infill = 15
    walls = 3
    layer = "0.20mm"
    supports = "None"
    tags = [file_format.replace(".", ""), "3dprint"]

    if any(k in lower_fn for k in ["benchy", "boat"]):
        category = "Calibration & Test"
        tags += ["benchy", "calibration", "benchmark", "hull"]
        infill = 10
        layer = "0.20mm"
        supports = "None"
        desc = "Classic 3D printer calibration benchmark boat designed to test overhangs, bridging, and dimensional accuracy."
    elif any(k in lower_fn for k in ["gear", "bracket", "hinge", "mount", "clamp", "adapter", "clip"]):
        category = "Tools & Utility"
        tags += ["functional", "mechanical", "hardware", "engineering"]
        mat = "PETG"
        infill = 35
        walls = 4
        supports = "Tree (auto)"
        desc = f"Functional mechanical component ({title}) optimized for load-bearing and dimensional durability."
    elif any(k in lower_fn for k in ["vase", "planter", "pot", "spiral"]):
        category = "Home & Living"
        tags += ["vase", "decorative", "interior", "container"]
        infill = 0
        walls = 2
        layer = "0.28mm"
        desc = f"Decorative container/vase model ({title}) suited for spiral vase mode or rapid aesthetic prints."
    elif any(k in lower_fn for k in ["figure", "mini", "statue", "dragon", "character", "toy"]):
        category = "Toys & Figures"
        tags += ["figurine", "miniature", "sculpture", "art"]
        layer = "0.12mm"
        infill = 15
        supports = "Tree (auto)"
        desc = f"High-detail miniature / figure ({title}) best printed with fine layer heights and tree supports."
    elif any(k in lower_fn for k in ["gridfinity", "drawer", "box", "organizer", "bin", "stand", "holder"]):
        category = "Desk & Organization"
        tags += ["organization", "storage", "desk", "modular"]
        infill = 15
        walls = 3
        desc = f"Modular organization container/stand ({title}) designed for tidy storage."
    else:
        desc = f"3D print asset {title} in {file_format.upper()} format. Ready for slicing on Bambu Lab P2S."

    # Adjust based on Z height or aspect ratio
    z = geometry.get("bbox_z_mm", 0)
    if z > 150:
        tags.append("tall")

    # Merge with smart semantic tag inference from name and description
    inferred = infer_tags_from_text(desc, title, file_format)
    final_tags = list(dict.fromkeys(tags + inferred))
    
    return {
        "name": title,
        "category": category,
        "tags": final_tags[:10],
        "description": desc,
        "material_recommendation": mat,
        "slicer_settings": {
            "layer_height": layer,
            "infill_percentage": infill,
            "infill_pattern": "Gyroid",
            "wall_loops": walls,
            "supports": supports,
            "bed_temp_c": 70 if mat == "PETG" else 60,
            "nozzle_temp_c": 240 if mat == "PETG" else 215,
            "brim": "Auto",
            "orientation_advice": "Place the flattest planar surface against the build plate."
        }
    }


def infer_tags_from_text(description: str, name: str, file_format: str = "") -> list:
    """
    Extracts and infers relevant 3D printing tags from a model's name and description.
    """
    text = f"{name} {description}".lower()

    STOP_WORDS = {
        "3d", "print", "printable", "asset", "format", "ready", "for", "slicing",
        "bambu", "lab", "p2s", "with", "and", "the", "this", "that", "from", "into",
        "over", "model", "file", "files", "designed", "optimized", "placed", "against",
        "build", "plate", "bed", "flattest", "planar", "surface", "component", "piece",
        "part", "parts", "item", "version", "final", "clean", "high", "low", "good",
        "very", "also", "used", "best", "printed"
    }

    tags = set()
    if file_format:
        fmt = file_format.replace(".", "").lower()
        if fmt:
            tags.add(fmt)

    KEYWORD_MAPPINGS = {
        ("shower", "bathroom", "bath", "tub", "faucet", "towel", "toilet"): ["bathroom", "home"],
        ("handle", "knob", "grip", "pull", "lever"): ["hardware", "handle"],
        ("basket", "caddy", "tray", "organizer", "bin", "box", "drawer", "holder"): ["storage", "organization"],
        ("vent", "grille", "duct", "hvac", "toe kick", "airflow", "fan"): ["hvac", "home-improvement", "vent"],
        ("blade", "cutter", "knife", "mower", "trimmer", "weed eater", "lawn", "garden"): ["tools", "yard", "replacement-part"],
        ("ryobi", "dewalt", "milwaukee", "makita", "bosch"): ["power-tools", "workshop"],
        ("stand", "mount", "bracket", "dock", "cradle"): ["mount", "stand", "desk"],
        ("spool", "adapter", "ams", "esun", "eryone", "cardboard"): ["3dprinter-mod", "spool-adapter", "ams-mod"],
        ("fidget", "toy", "spinner", "articulated", "flexi", "shark", "lizard"): ["toy", "fidget", "articulated"],
        ("dragon", "creature", "monster", "miniature", "figure", "statue"): ["miniature", "figurine", "art"],
        ("mtg", "magic", "deck", "counter", "card", "token"): ["tabletop", "mtg", "gaming"],
        ("doorbell", "ring", "camera", "angle", "wall mount"): ["smarthome", "mounting", "electronics"],
        ("tube", "pipe", "conduit", "hose", "fitting"): ["plumbing", "hardware"]
    }

    for triggers, implied_tags in KEYWORD_MAPPINGS.items():
        if any(trigger in text for trigger in triggers):
            tags.update(implied_tags)

    words = re.findall(r"[a-z0-9]{3,}", text)
    for w in words:
        if w not in STOP_WORDS and not w.isdigit() and len(w) <= 18:
            tags.add(w)

    tags.add("3dprint")

    sorted_tags = sorted(
        list(tags),
        key=lambda t: (t in ["3dprint", "3mf", "stl"], -len(t))
    )
    return sorted_tags[:10]
