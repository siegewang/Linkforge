from flask import Blueprint, render_template, jsonify, request
import sqlite3
import logging
from services.db import get_db, retry_write

logger = logging.getLogger(__name__)
notes_bp = Blueprint('notes', __name__)

@notes_bp.route("/notes")
def notes_page():
    return render_template("notes.html", active_page='notes')

@notes_bp.route("/api/notes", methods=["GET", "POST"])
def api_notes():
    if request.method == "GET":
        conn = get_db()
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM notes ORDER BY is_done ASC, date_added DESC").fetchall()
        return jsonify([dict(r) for r in rows])
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        content = str(data.get("content", "")).strip()
        category = str(data.get("category", "note"))
        if not content:
            return jsonify({"status": "empty"}), 400
        def _write():
            conn = get_db()
            conn.execute("INSERT INTO notes (content, category) VALUES (?, ?)", (content, category))
            conn.commit()
        retry_write(_write)
        return jsonify({"status": "added"})

@notes_bp.route("/api/notes/<int:note_id>", methods=["PUT", "DELETE"])
def api_note_action(note_id):
    if request.method == "DELETE":
        def _del():
            conn = get_db()
            conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
            conn.commit()
        retry_write(_del)
        return jsonify({"status": "deleted"})
    data = request.get_json(silent=True) or {}
    def _update():
        conn = get_db()
        if "is_done" in data:
            conn.execute("UPDATE notes SET is_done = ? WHERE id = ?", (int(data["is_done"]), note_id))
            conn.commit()
    retry_write(_update)
    return jsonify({"status": "updated"})
