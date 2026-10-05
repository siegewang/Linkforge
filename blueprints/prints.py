import os
import shutil
import json
import logging
import re
from pathlib import Path
from typing import Optional, List, Dict, Any
from flask import Blueprint, render_template, request, jsonify, send_file, Response, current_app
from config import Config
from services.db import get_db, retry_write
from services.geometry import parse_geometry
from services.bambu_cloud import bambu_cloud
from services.gemini_print_service import analyze_model_with_gemini, infer_tags_from_text, batch_auto_categorize_models
from services.print_thumbnails import (
    extract_embedded_3mf_thumbnail,
    save_thumbnail_from_data_url,
    generate_placeholder_thumbnail,
    get_thumbnails_dir
)

logger = logging.getLogger(__name__)

prints_bp = Blueprint("prints", __name__)

ALLOWED_EXTENSIONS = {".stl", ".3mf", ".obj", ".step", ".stp"}
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".webm", ".mkv"}

def get_models_dir() -> Path:
    d = Path(getattr(Config, "MODELS_DIR", os.path.join(os.path.dirname(os.path.abspath(Config.DB_PATH)), "models")))
    d.mkdir(parents=True, exist_ok=True)
    return d

def get_timelapses_dir() -> Path:
    d = Path(getattr(Config, "TIMELAPSES_DIR", os.path.join(os.path.dirname(os.path.abspath(Config.DB_PATH)), "timelapses")))
    d.mkdir(parents=True, exist_ok=True)
    return d

def _format_model_row(row, base_url: str) -> dict:
    clean_base = base_url.rstrip("/")
    m_id = row["id"]
    filename = row["filename"]
    thumb_path = row["thumbnail_path"]
    
    download_url = f"{clean_base}/api/models/{m_id}/download/{filename}"
    v_param = f"?v={thumb_path}" if thumb_path else ""
    thumbnail_url = f"/api/models/{m_id}/thumbnail{v_param}" if thumb_path else None
    
    # Custom URI schemes for desktop apps
    bambu_studio_uri = f"bambustudio://open?file={download_url}"
    fusion360_uri = f"fusion360://open?file={download_url}"

    # Parse JSON fields safely
    tags = []
    if row["tags"]:
        try:
            tags = json.loads(row["tags"]) if isinstance(row["tags"], str) and row["tags"].startswith("[") else [t.strip() for t in row["tags"].split(",") if t.strip()]
        except Exception:
            tags = [row["tags"]]

    slicer_settings = {}
    if row["slicer_settings"]:
        try:
            slicer_settings = json.loads(row["slicer_settings"]) if isinstance(row["slicer_settings"], str) else row["slicer_settings"]
        except Exception:
            slicer_settings = {}

    geometry_metadata = {}
    if row["geometry_metadata"]:
        try:
            geometry_metadata = json.loads(row["geometry_metadata"]) if isinstance(row["geometry_metadata"], str) else row["geometry_metadata"]
        except Exception:
            geometry_metadata = {}

    # Query linked timelapses
    timelapses_data = []
    try:
        conn = get_db()
        tl_rows = conn.execute("SELECT * FROM timelapses WHERE model_id = ? ORDER BY created_at DESC", (m_id,)).fetchall()
        for t in tl_rows:
            timelapses_data.append({
                "id": t["id"],
                "model_id": t["model_id"],
                "title": t["title"],
                "filename": t["filename"],
                "file_size": t["file_size"],
                "duration_seconds": t["duration_seconds"],
                "created_at": str(t["created_at"]),
                "stream_url": f"{clean_base}/api/timelapses/{t['id']}/stream"
            })
    except Exception:
        pass

    return {
        "id": m_id,
        "name": row["name"],
        "filename": filename,
        "file_size": row["file_size"] or 0,
        "file_format": row["file_format"],
        "thumbnail_path": thumb_path,
        "thumbnail_url": thumbnail_url,
        "download_url": download_url,
        "bambu_studio_uri": bambu_studio_uri,
        "fusion360_uri": fusion360_uri,
        "description": row["description"] or "",
        "category": row["category"] or "General",
        "material_recommendation": row["material_recommendation"] or "PLA",
        "tags": tags,
        "slicer_settings": slicer_settings,
        "geometry_metadata": geometry_metadata,
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "timelapses": timelapses_data
    }


# --- Web Page Route ---
@prints_bp.route("/prints", methods=["GET"])
def prints_page():
    return render_template("prints.html", active_page="prints")


# --- Categories & Taxonomy API ---
@prints_bp.route("/api/models/categories", methods=["GET"])
def get_categories():
    conn = get_db()
    cat_rows = conn.execute("SELECT * FROM print_categories ORDER BY display_order ASC, id ASC").fetchall()
    counts_rows = conn.execute("SELECT category, COUNT(*) as count FROM model_assets GROUP BY category").fetchall()
    counts_map = {r["category"]: r["count"] for r in counts_rows if r["category"]}
    total_models = conn.execute("SELECT COUNT(*) FROM model_assets").fetchone()[0]

    categories = []
    for r in cat_rows:
        categories.append({
            "id": r["id"],
            "name": r["name"],
            "slug": r["slug"],
            "icon": r["icon"] or "fa-folder",
            "color": r["color"] or "#6366f1",
            "description": r["description"] or "",
            "display_order": r["display_order"] or 0,
            "count": counts_map.get(r["name"], 0)
        })

    return jsonify({
        "categories": categories,
        "total_count": total_models
    })


@prints_bp.route("/api/models/categories", methods=["POST"])
def create_category():
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Category name is required"}), 400

    slug = (data.get("slug") or re.sub(r'[^a-zA-Z0-9_-]', '-', name.lower())).strip("-")
    icon = (data.get("icon") or "fa-folder").strip()
    color = (data.get("color") or "#6366f1").strip()
    description = (data.get("description") or "").strip()
    display_order = data.get("display_order", 99)

    def _insert():
        c = get_db()
        cursor = c.cursor()
        cursor.execute("""
            INSERT INTO print_categories (name, slug, icon, color, description, display_order)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (name, slug, icon, color, description, display_order))
        c.commit()
        return cursor.lastrowid

    try:
        new_id = retry_write(_insert)
        conn = get_db()
        row = conn.execute("SELECT * FROM print_categories WHERE id = ?", (new_id,)).fetchone()
        return jsonify({
            "id": row["id"],
            "name": row["name"],
            "slug": row["slug"],
            "icon": row["icon"],
            "color": row["color"],
            "description": row["description"],
            "display_order": row["display_order"],
            "count": 0
        }), 201
    except Exception as e:
        return jsonify({"error": f"Failed to create category: {e}"}), 400


@prints_bp.route("/api/models/categories/<int:cat_id>", methods=["PUT", "POST"])
def update_category(cat_id):
    data = request.get_json() or {}
    conn = get_db()
    existing = conn.execute("SELECT * FROM print_categories WHERE id = ?", (cat_id,)).fetchone()
    if not existing:
        return jsonify({"error": "Category not found"}), 404

    old_name = existing["name"]
    name = (data.get("name") or existing["name"]).strip()
    slug = (data.get("slug") or existing["slug"]).strip()
    icon = (data.get("icon") or existing["icon"]).strip()
    color = (data.get("color") or existing["color"]).strip()
    description = data.get("description", existing["description"])
    display_order = data.get("display_order", existing["display_order"])

    def _update():
        c = get_db()
        c.execute("""
            UPDATE print_categories 
            SET name = ?, slug = ?, icon = ?, color = ?, description = ?, display_order = ?
            WHERE id = ?
        """, (name, slug, icon, color, description, display_order, cat_id))
        if old_name != name:
            c.execute("UPDATE model_assets SET category = ? WHERE category = ?", (name, old_name))
        c.commit()

    retry_write(_update)
    updated = conn.execute("SELECT * FROM print_categories WHERE id = ?", (cat_id,)).fetchone()
    count = conn.execute("SELECT COUNT(*) FROM model_assets WHERE category = ?", (name,)).fetchone()[0]
    return jsonify({
        "id": updated["id"],
        "name": updated["name"],
        "slug": updated["slug"],
        "icon": updated["icon"],
        "color": updated["color"],
        "description": updated["description"],
        "display_order": updated["display_order"],
        "count": count
    })


@prints_bp.route("/api/models/categories/<int:cat_id>", methods=["DELETE"])
def delete_category(cat_id):
    conn = get_db()
    existing = conn.execute("SELECT * FROM print_categories WHERE id = ?", (cat_id,)).fetchone()
    if not existing:
        return jsonify({"error": "Category not found"}), 404

    cat_name = existing["name"]

    def _delete():
        c = get_db()
        c.execute("DELETE FROM print_categories WHERE id = ?", (cat_id,))
        c.execute("UPDATE model_assets SET category = 'General & Other' WHERE category = ?", (cat_name,))
        c.commit()

    retry_write(_delete)
    return jsonify({"success": True, "message": f"Category '{cat_name}' deleted and models moved to 'General & Other'"})


# --- Tags Management API ---
@prints_bp.route("/api/models/tags", methods=["GET"])
def get_all_tags():
    conn = get_db()
    rows = conn.execute("SELECT tags FROM model_assets WHERE tags IS NOT NULL").fetchall()
    tag_counts = {}
    for r in rows:
        raw = r["tags"]
        if not raw:
            continue
        try:
            tags = json.loads(raw) if isinstance(raw, str) and raw.startswith("[") else [t.strip() for t in raw.split(",") if t.strip()]
        except Exception:
            tags = [raw]
        if isinstance(tags, list):
            for t in tags:
                if isinstance(t, str):
                    clean_t = t.strip().lower().replace("#", "")
                    if clean_t:
                        tag_counts[clean_t] = tag_counts.get(clean_t, 0) + 1
    sorted_tags = sorted([{"name": k, "count": v} for k, v in tag_counts.items()], key=lambda x: (-x["count"], x["name"]))
    return jsonify({
        "tags": sorted_tags,
        "total_unique": len(sorted_tags)
    })


@prints_bp.route("/api/models/tags/<path:tag_name>", methods=["DELETE"])
def delete_global_tag(tag_name):
    clean_tag = tag_name.strip().lower().replace("#", "")
    if not clean_tag:
        return jsonify({"error": "Invalid tag name"}), 400

    conn = get_db()
    rows = conn.execute("SELECT id, tags FROM model_assets WHERE tags IS NOT NULL").fetchall()
    affected_ids = []

    def _delete_tag():
        nonlocal affected_ids
        c = get_db()
        for r in rows:
            raw = r["tags"]
            if not raw:
                continue
            try:
                tags = json.loads(raw) if isinstance(raw, str) and raw.startswith("[") else [t.strip() for t in raw.split(",") if t.strip()]
            except Exception:
                tags = [raw]
            if isinstance(tags, list):
                new_tags = [t for t in tags if isinstance(t, str) and t.strip().lower().replace("#", "") != clean_tag]
                if len(new_tags) != len(tags):
                    c.execute("UPDATE model_assets SET tags = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (json.dumps(new_tags), r["id"]))
                    affected_ids.append(r["id"])
        c.commit()

    retry_write(_delete_tag)
    return jsonify({
        "success": True,
        "tag": clean_tag,
        "affected_models_count": len(affected_ids),
        "message": f"Tag '#{clean_tag}' deleted from {len(affected_ids)} model(s)"
    })


@prints_bp.route("/api/models/batch-categorize", methods=["POST"])
def batch_categorize():
    data = request.get_json() or {}
    model_ids = data.get("model_ids", [])
    target_category = (data.get("category") or "").strip()

    if not model_ids or not target_category:
        return jsonify({"error": "model_ids list and category are required"}), 400

    clean_ids = [int(mid) for mid in model_ids if str(mid).isdigit()]
    if not clean_ids:
        return jsonify({"error": "No valid model IDs provided"}), 400

    def _batch_update():
        c = get_db()
        placeholders = ",".join(["?"] * len(clean_ids))
        c.execute(f"""
            UPDATE model_assets 
            SET category = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id IN ({placeholders})
        """, [target_category] + clean_ids)
        c.commit()

    retry_write(_batch_update)
    return jsonify({
        "success": True,
        "updated_count": len(clean_ids),
        "target_category": target_category
    })


@prints_bp.route("/api/ai/auto-categorize-models", methods=["POST"])
def auto_categorize_models():
    data = request.get_json() or {}
    target_model_ids = data.get("model_ids")

    conn = get_db()
    categories_rows = conn.execute("SELECT name FROM print_categories ORDER BY display_order ASC").fetchall()
    categories = [r["name"] for r in categories_rows]
    if not categories:
        categories = ["Workshop & Jigs", "Desk & Gridfinity", "Electronics & Enclosures", "Home & Utility", "Art & Minis", "Multi-Plate Assemblies", "Calibration & Benchies", "General & Other"]

    query = "SELECT * FROM model_assets"
    params = []
    if target_model_ids and isinstance(target_model_ids, list):
        placeholders = ",".join(["?"] * len(target_model_ids))
        query += f" WHERE id IN ({placeholders})"
        params = target_model_ids

    rows = conn.execute(query, params).fetchall()
    models_to_categorize = []
    for r in rows:
        tags = []
        if r["tags"]:
            try:
                tags = json.loads(r["tags"]) if isinstance(r["tags"], str) and r["tags"].startswith("[") else [t.strip() for t in r["tags"].split(",") if t.strip()]
            except Exception:
                tags = [r["tags"]]
        models_to_categorize.append({
            "id": r["id"],
            "name": r["name"],
            "filename": r["filename"],
            "description": r["description"] or "",
            "tags": tags
        })

    classifications = batch_auto_categorize_models(models_to_categorize, categories)

    def _apply_classifications():
        c = get_db()
        for mid, cat in classifications.items():
            c.execute("UPDATE model_assets SET category = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (cat, mid))
        c.commit()

    if classifications:
        retry_write(_apply_classifications)

    return jsonify({
        "success": True,
        "categorized_count": len(classifications),
        "classifications": classifications
    })


# --- Model Assets API ---
@prints_bp.route("/api/models", methods=["GET"])
def list_models():
    search = request.args.get("search", "").strip()
    category = request.args.get("category", "").strip()
    material = request.args.get("material", "").strip()
    sort_by = request.args.get("sort_by", "created_at_desc")

    conn = get_db()
    query = "SELECT * FROM model_assets WHERE 1=1"
    params = []

    if category and category.lower() != "all":
        query += " AND category = ?"
        params.append(category)

    if material and material.lower() != "all":
        query += " AND material_recommendation = ?"
        params.append(material)

    if search:
        search_like = f"%{search}%"
        query += " AND (name LIKE ? OR description LIKE ? OR filename LIKE ? OR tags LIKE ?)"
        params.extend([search_like, search_like, search_like, search_like])

    if sort_by == "created_at_asc":
        query += " ORDER BY created_at ASC"
    elif sort_by == "name_asc":
        query += " ORDER BY name COLLATE NOCASE ASC"
    elif sort_by == "size_desc":
        query += " ORDER BY file_size DESC"
    else:
        query += " ORDER BY created_at DESC"

    rows = conn.execute(query, params).fetchall()
    base_url = request.host_url.rstrip("/")

    return jsonify([_format_model_row(r, base_url) for r in rows])


@prints_bp.route("/api/models/<int:model_id>", methods=["GET"])
def get_model(model_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM model_assets WHERE id = ?", (model_id,)).fetchone()
    if not row:
        return jsonify({"error": "Model not found"}), 404

    base_url = request.host_url.rstrip("/")
    return jsonify(_format_model_row(row, base_url))


@prints_bp.route("/api/models/upload", methods=["POST"])
def upload_models():
    files = request.files.getlist("files") or request.files.getlist("files[]")
    if not files:
        if "file" in request.files:
            files = [request.files["file"]]

    if not files:
        return jsonify({"error": "No files provided"}), 400

    custom_instructions = request.form.get("custom_instructions", "")
    
    # Optional per-file custom name and category overrides
    file_names_raw = request.form.get("file_names", "{}")
    file_categories_raw = request.form.get("file_categories", "{}")
    try:
        custom_names_map = json.loads(file_names_raw) if file_names_raw else {}
    except Exception:
        custom_names_map = {}
    try:
        custom_categories_map = json.loads(file_categories_raw) if file_categories_raw else {}
    except Exception:
        custom_categories_map = {}

    created_models = []
    models_dir = get_models_dir()
    base_url = request.host_url.rstrip("/")

    for f in files:
        if not f or not f.filename:
            continue

        ext = Path(f.filename).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            continue

        dest_filename = f"{Path(f.filename).stem}_{os.urandom(4).hex()}{ext}"
        target_path = models_dir / dest_filename
        f.save(str(target_path))

        file_size = target_path.stat().st_size

        # 1. Parse geometry
        geometry = parse_geometry(target_path)

        # 2. Analyze with Gemini AI
        ai_data = analyze_model_with_gemini(
            filename=f.filename,
            file_format=ext,
            geometry=geometry,
            custom_instructions=custom_instructions
        )

        # Apply custom name override if specified by user
        user_specified_name = custom_names_map.get(f.filename)
        model_name = user_specified_name.strip() if user_specified_name and user_specified_name.strip() else ai_data["name"]

        # Apply custom category override if specified by user and not "Auto"
        user_specified_category = custom_categories_map.get(f.filename)
        if user_specified_category and user_specified_category.strip() and user_specified_category.lower() != "auto":
            model_category = user_specified_category.strip()
        else:
            model_category = ai_data["category"]

        # 3. Save model record
        def _insert_model():
            c = get_db()
            cursor = c.cursor()
            cursor.execute("""
                INSERT INTO model_assets (
                    name, filename, file_path, file_size, file_format,
                    thumbnail_path, description, tags, category,
                    material_recommendation, slicer_settings, geometry_metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                model_name,
                f.filename,
                dest_filename,
                file_size,
                ext.replace(".", ""),
                None,
                ai_data["description"],
                json.dumps(ai_data["tags"]),
                model_category,
                ai_data["material_recommendation"],
                json.dumps(ai_data["slicer_settings"]),
                json.dumps(geometry)
            ))
            c.commit()
            return cursor.lastrowid

        model_id = retry_write(_insert_model)

        # 4. Extract embedded thumbnail (for 3MF files) or generate placeholder
        thumbnail_file = None
        if ext == ".3mf":
            thumbnail_file = extract_embedded_3mf_thumbnail(target_path, model_id)

        if not thumbnail_file:
            thumbnail_file = generate_placeholder_thumbnail(model_id, model_name, ext)

        if thumbnail_file:
            def _update_thumb():
                c = get_db()
                c.execute("UPDATE model_assets SET thumbnail_path = ? WHERE id = ?", (thumbnail_file, model_id))
                c.commit()
            retry_write(_update_thumb)

        conn = get_db()
        row = conn.execute("SELECT * FROM model_assets WHERE id = ?", (model_id,)).fetchone()
        created_models.append(_format_model_row(row, base_url))

    return jsonify(created_models)


@prints_bp.route("/api/models/<int:model_id>", methods=["PUT", "POST"])
def update_model(model_id):
    data = request.get_json() or {}
    conn = get_db()
    row = conn.execute("SELECT * FROM model_assets WHERE id = ?", (model_id,)).fetchone()
    if not row:
        return jsonify({"error": "Model not found"}), 404

    name = data.get("name", row["name"])
    description = data.get("description", row["description"])
    category = data.get("category", row["category"])
    material = data.get("material_recommendation", row["material_recommendation"])
    
    tags = data.get("tags")
    tags_str = json.dumps(tags) if tags is not None else row["tags"]

    slicer_settings = data.get("slicer_settings")
    slicer_str = json.dumps(slicer_settings) if slicer_settings is not None else row["slicer_settings"]

    def _update():
        c = get_db()
        c.execute("""
            UPDATE model_assets SET 
                name = ?, description = ?, category = ?, 
                material_recommendation = ?, tags = ?, slicer_settings = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (name, description, category, material, tags_str, slicer_str, model_id))
        c.commit()

    retry_write(_update)
    updated_row = conn.execute("SELECT * FROM model_assets WHERE id = ?", (model_id,)).fetchone()
    base_url = request.host_url.rstrip("/")
    return jsonify(_format_model_row(updated_row, base_url))


@prints_bp.route("/api/models/<int:model_id>", methods=["DELETE"])
def delete_model(model_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM model_assets WHERE id = ?", (model_id,)).fetchone()
    if not row:
        return jsonify({"error": "Model not found"}), 404

    # Remove files on disk
    models_dir = get_models_dir()
    thumbs_dir = get_thumbnails_dir()

    try:
        model_file = models_dir / row["file_path"]
        if model_file.exists():
            model_file.unlink()
        if row["thumbnail_path"]:
            thumb_file = thumbs_dir / row["thumbnail_path"]
            if thumb_file.exists():
                thumb_file.unlink()
    except Exception as e:
        logger.warning(f"Error removing file from disk for model {model_id}: {e}")

    def _delete():
        c = get_db()
        c.execute("DELETE FROM model_assets WHERE id = ?", (model_id,))
        c.commit()

    retry_write(_delete)
    return jsonify({"success": True, "message": "Model deleted successfully"})


@prints_bp.route("/api/models/<int:model_id>/download", methods=["GET"])
@prints_bp.route("/api/models/<int:model_id>/download/<filename>", methods=["GET"])
def download_model(model_id, filename=None):
    conn = get_db()
    row = conn.execute("SELECT * FROM model_assets WHERE id = ?", (model_id,)).fetchone()
    if not row:
        return jsonify({"error": "Model not found"}), 404

    models_dir = get_models_dir()
    file_path = models_dir / row["file_path"]
    if not file_path.exists():
        return jsonify({"error": "Physical file missing on server"}), 404

    return send_file(
        str(file_path),
        as_attachment=True,
        download_name=row["filename"],
        mimetype="application/octet-stream"
    )


@prints_bp.route("/api/models/<int:model_id>/file", methods=["GET"])
def get_raw_model_file(model_id):
    """Serves the 3D file for Three.js client loading with CORS."""
    conn = get_db()
    row = conn.execute("SELECT * FROM model_assets WHERE id = ?", (model_id,)).fetchone()
    if not row:
        return jsonify({"error": "Model not found"}), 404

    models_dir = get_models_dir()
    file_path = models_dir / row["file_path"]
    if not file_path.exists():
        return jsonify({"error": "Model file not found on disk"}), 404

    ext = (row["file_format"] or "").lower()
    if ext == "stl":
        media_type = "model/stl"
    elif ext == "3mf":
        media_type = "model/3mf"
    elif ext == "obj":
        media_type = "model/obj"
    elif ext in ("step", "stp"):
        media_type = "model/step"
    else:
        media_type = "application/octet-stream"

    resp = send_file(str(file_path), mimetype=media_type)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@prints_bp.route("/api/models/<int:model_id>/thumbnail", methods=["GET"])
def get_thumbnail(model_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM model_assets WHERE id = ?", (model_id,)).fetchone()
    if not row or not row["thumbnail_path"]:
        return jsonify({"error": "Thumbnail not found"}), 404

    thumbs_dir = get_thumbnails_dir()
    thumb_file = thumbs_dir / row["thumbnail_path"]
    if not thumb_file.exists():
        return jsonify({"error": "Thumbnail file not found on disk"}), 404

    resp = send_file(str(thumb_file), mimetype="image/webp")
    resp.headers["Cache-Control"] = "public, max-age=3600, must-revalidate"
    return resp


@prints_bp.route("/api/models/<int:model_id>/thumbnail", methods=["POST"])
def upload_thumbnail(model_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM model_assets WHERE id = ?", (model_id,)).fetchone()
    if not row:
        return jsonify({"error": "Model not found"}), 404

    saved_filename = None
    data_url = request.form.get("data_url") or (request.json.get("data_url") if request.is_json else None)
    
    if data_url:
        saved_filename = save_thumbnail_from_data_url(data_url, model_id)
    elif "file" in request.files:
        f = request.files["file"]
        saved_filename = f"thumb_{model_id}_{os.urandom(4).hex()}.webp"
        target_path = get_thumbnails_dir() / saved_filename
        f.save(str(target_path))

    if saved_filename:
        def _update():
            c = get_db()
            c.execute("UPDATE model_assets SET thumbnail_path = ? WHERE id = ?", (saved_filename, model_id))
            c.commit()
        retry_write(_update)
        return jsonify({"success": True, "thumbnail_url": f"/api/models/{model_id}/thumbnail?v={saved_filename}"})

    return jsonify({"error": "Failed to process thumbnail"}), 400


@prints_bp.route("/api/models/<int:model_id>/infer-tags", methods=["POST"])
def infer_model_tags(model_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM model_assets WHERE id = ?", (model_id,)).fetchone()
    if not row:
        return jsonify({"error": "Model not found"}), 404

    new_tags = infer_tags_from_text(
        description=row["description"] or "",
        name=row["name"] or "",
        file_format=row["file_format"] or ""
    )

    def _update():
        c = get_db()
        c.execute("UPDATE model_assets SET tags = ? WHERE id = ?", (json.dumps(new_tags), model_id))
        c.commit()

    retry_write(_update)
    updated_row = conn.execute("SELECT * FROM model_assets WHERE id = ?", (model_id,)).fetchone()
    base_url = request.host_url.rstrip("/")
    return jsonify(_format_model_row(updated_row, base_url))


@prints_bp.route("/api/models/backfill-thumbnails", methods=["POST"])
def backfill_thumbnails():
    conn = get_db()
    rows = conn.execute("SELECT * FROM model_assets").fetchall()
    updated_count = 0
    models_dir = get_models_dir()

    for row in rows:
        file_path = models_dir / row["file_path"]
        if not file_path.exists():
            continue

        if row["file_format"] == "3mf":
            extracted = extract_embedded_3mf_thumbnail(file_path, row["id"])
            if extracted:
                def _up(rid=row["id"], thumb=extracted):
                    c = get_db()
                    c.execute("UPDATE model_assets SET thumbnail_path = ? WHERE id = ?", (thumb, rid))
                    c.commit()
                retry_write(_up)
                updated_count += 1

    return jsonify({"success": True, "updated_models": updated_count, "total_models": len(rows)})


# --- Printer & AMS API ---
@prints_bp.route("/api/printer/status", methods=["GET"])
def get_printer_status():
    status = bambu_cloud.get_printer_status()
    return jsonify(status)


@prints_bp.route("/api/printer/ams", methods=["GET"])
def get_ams_info():
    status = bambu_cloud.get_printer_status()
    return jsonify(status.get("ams", {}))


@prints_bp.route("/api/printer/send", methods=["POST"])
def send_to_printer():
    data = request.get_json() or {}
    model_id = data.get("model_id")
    if not model_id:
        return jsonify({"error": "model_id is required"}), 400

    conn = get_db()
    row = conn.execute("SELECT * FROM model_assets WHERE id = ?", (model_id,)).fetchone()
    if not row:
        return jsonify({"error": "Model not found"}), 404

    base_url = request.host_url.rstrip("/")
    download_url = f"{base_url}/api/models/{row['id']}/download"

    dispatch_result = bambu_cloud.send_print_task(
        file_download_url=download_url,
        filename=row["filename"],
        ams_slot=data.get("ams_tray_id", 1),
        bed_type=data.get("bed_type", "textured_plate"),
        auto_bed_leveling=data.get("auto_bed_leveling", True),
        flow_cali=data.get("flow_cali", True),
        timelapse_enabled=data.get("timelapse_enabled", True),
        use_ams=data.get("use_ams", True)
    )

    return jsonify({
        "success": dispatch_result.get("success", True),
        "task_id": dispatch_result.get("task_id"),
        "message": dispatch_result.get("message", "Sent to Bambu Cloud"),
        "printer_state": dispatch_result.get("printer_state", "READY")
    })


# --- Timelapse Recording API ---
@prints_bp.route("/api/timelapses/upload", methods=["POST"])
def upload_timelapse():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    f = request.files["file"]
    if not f or not f.filename:
        return jsonify({"error": "Empty filename"}), 400

    ext = Path(f.filename).suffix.lower()
    if ext not in ALLOWED_VIDEO_EXTENSIONS:
        return jsonify({"error": f"Unsupported video format. Allowed: {', '.join(ALLOWED_VIDEO_EXTENSIONS)}"}), 400

    model_id = request.form.get("model_id")
    model_id = int(model_id) if model_id and str(model_id).isdigit() else None
    title = request.form.get("title") or f.filename

    timelapses_dir = get_timelapses_dir()
    dest_filename = f"timelapse_{os.urandom(6).hex()}{ext}"
    target_path = timelapses_dir / dest_filename
    f.save(str(target_path))

    file_size = target_path.stat().st_size

    def _insert():
        c = get_db()
        cursor = c.cursor()
        cursor.execute("""
            INSERT INTO timelapses (model_id, title, filename, file_path, file_size)
            VALUES (?, ?, ?, ?, ?)
        """, (model_id, title, f.filename, dest_filename, file_size))
        c.commit()
        return cursor.lastrowid

    tl_id = retry_write(_insert)
    base_url = request.host_url.rstrip("/")

    return jsonify({
        "id": tl_id,
        "model_id": model_id,
        "title": title,
        "filename": f.filename,
        "file_size": file_size,
        "stream_url": f"{base_url}/api/timelapses/{tl_id}/stream"
    })


@prints_bp.route("/api/timelapses/<int:timelapse_id>/stream", methods=["GET"])
def stream_timelapse(timelapse_id):
    """Streams video file with HTTP Range support for HTML5 video player."""
    conn = get_db()
    row = conn.execute("SELECT * FROM timelapses WHERE id = ?", (timelapse_id,)).fetchone()
    if not row:
        return jsonify({"error": "Timelapse not found"}), 404

    timelapses_dir = get_timelapses_dir()
    video_path = timelapses_dir / row["file_path"]
    if not video_path.exists():
        return jsonify({"error": "Video file missing from storage"}), 404

    file_size = video_path.stat().st_size
    range_header = request.headers.get("Range", None)
    content_type = "video/webm" if video_path.suffix.lower() == ".webm" else "video/mp4"

    if range_header:
        byte1, byte2 = 0, None
        m = re.search(r"(\d+)-(\d*)", range_header)
        if m:
            groups = m.groups()
            byte1 = int(groups[0])
            if groups[1]:
                byte2 = int(groups[1])
            else:
                byte2 = file_size - 1

        length = byte2 - byte1 + 1

        def generate():
            with open(video_path, "rb") as f:
                f.seek(byte1)
                remaining = length
                chunk_size = 1024 * 1024
                while remaining > 0:
                    read_len = min(remaining, chunk_size)
                    data = f.read(read_len)
                    if not data:
                        break
                    remaining -= len(data)
                    yield data

        rv = Response(generate(), 206, mimetype=content_type, direct_passthrough=True)
        rv.headers.add("Content-Range", f"bytes {byte1}-{byte2}/{file_size}")
        rv.headers.add("Accept-Ranges", "bytes")
        rv.headers.add("Content-Length", str(length))
        return rv
    else:
        return send_file(str(video_path), mimetype=content_type)


@prints_bp.route("/api/timelapses/<int:timelapse_id>", methods=["DELETE"])
def delete_timelapse(timelapse_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM timelapses WHERE id = ?", (timelapse_id,)).fetchone()
    if not row:
        return jsonify({"error": "Timelapse not found"}), 404

    timelapses_dir = get_timelapses_dir()
    video_path = timelapses_dir / row["file_path"]
    if video_path.exists():
        try:
            video_path.unlink()
        except Exception:
            pass

    def _del():
        c = get_db()
        c.execute("DELETE FROM timelapses WHERE id = ?", (timelapse_id,))
        c.commit()

    retry_write(_del)
    return jsonify({"success": True, "message": "Timelapse removed"})


# --- AI Analysis API ---
@prints_bp.route("/api/ai/analyze/<int:model_id>", methods=["POST"])
def reanalyze_model(model_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM model_assets WHERE id = ?", (model_id,)).fetchone()
    if not row:
        return jsonify({"error": "Model not found"}), 404

    data = request.get_json() or {}
    custom_instructions = data.get("custom_instructions")

    geometry = {}
    if row["geometry_metadata"]:
        try:
            geometry = json.loads(row["geometry_metadata"]) if isinstance(row["geometry_metadata"], str) else row["geometry_metadata"]
        except Exception:
            geometry = {}

    ai_result = analyze_model_with_gemini(
        filename=row["filename"],
        file_format=f".{row['file_format']}",
        geometry=geometry,
        custom_instructions=custom_instructions
    )

    def _update():
        c = get_db()
        c.execute("""
            UPDATE model_assets SET 
                name = ?, category = ?, tags = ?, 
                description = ?, material_recommendation = ?, slicer_settings = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (
            ai_result["name"],
            ai_result["category"],
            json.dumps(ai_result["tags"]),
            ai_result["description"],
            ai_result["material_recommendation"],
            json.dumps(ai_result["slicer_settings"]),
            model_id
        ))
        c.commit()

    retry_write(_update)
    updated_row = conn.execute("SELECT * FROM model_assets WHERE id = ?", (model_id,)).fetchone()
    base_url = request.host_url.rstrip("/")
    return jsonify(_format_model_row(updated_row, base_url))
