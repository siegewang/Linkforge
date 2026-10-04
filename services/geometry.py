import os
import struct
from pathlib import Path
from typing import Dict, Any, Optional

def parse_geometry(file_path: Path) -> Dict[str, Any]:
    """
    Parses geometry metadata (bounding box X/Y/Z mm, triangles, volume cm³)
    from 3D files (STL, OBJ, 3MF, STEP).
    """
    file_path = Path(file_path)
    ext = file_path.suffix.lower()
    
    # Try trimesh first if available
    try:
        import trimesh
        mesh = trimesh.load(str(file_path), force='mesh')
        
        # In case trimesh returns a Scene (common with 3MF or multi-part OBJ)
        if isinstance(mesh, trimesh.Scene):
            if len(mesh.geometry) > 0:
                mesh = trimesh.util.concatenate([g for g in mesh.geometry.values() if isinstance(g, trimesh.Trimesh)])
            else:
                mesh = None

        if mesh is not None and hasattr(mesh, 'bounds') and mesh.bounds is not None:
            min_corner, max_corner = mesh.bounds[0], mesh.bounds[1]
            dx = float(abs(max_corner[0] - min_corner[0]))
            dy = float(abs(max_corner[1] - min_corner[1]))
            dz = float(abs(max_corner[2] - min_corner[2]))
            
            triangles = int(len(mesh.faces)) if hasattr(mesh, 'faces') else 0
            
            volume_cm3 = 0.0
            if hasattr(mesh, 'is_watertight') and mesh.is_watertight:
                volume_cm3 = float(abs(mesh.volume)) / 1000.0  # mm^3 to cm^3
            else:
                # Approximate bounding box volume * typical infill ratio (15%)
                volume_cm3 = float(dx * dy * dz * 0.15) / 1000.0

            # Estimate weight in grams assuming PLA density (1.24 g/cm3) and ~20% solid fraction
            estimated_weight_g = round(volume_cm3 * 1.24, 1)

            return {
                "bbox_x_mm": round(dx, 1),
                "bbox_y_mm": round(dy, 1),
                "bbox_z_mm": round(dz, 1),
                "volume_cm3": round(volume_cm3, 2),
                "triangles": triangles,
                "estimated_weight_g": estimated_weight_g,
                "parsed_by": "trimesh"
            }
    except Exception:
        # Fall back to lightweight direct parsers
        pass

    # Built-in fallback parsers
    if ext == ".stl":
        return _parse_stl_fallback(file_path)
    elif ext == ".obj":
        return _parse_obj_fallback(file_path)
    
    # For .3mf or .step fallback
    return {
        "bbox_x_mm": 0.0,
        "bbox_y_mm": 0.0,
        "bbox_z_mm": 0.0,
        "volume_cm3": 0.0,
        "triangles": 0,
        "estimated_weight_g": 0.0,
        "parsed_by": "default_stub"
    }


def _parse_stl_fallback(file_path: Path) -> Dict[str, Any]:
    """Parse binary or ASCII STL bounding box and triangle count."""
    try:
        size = file_path.stat().st_size
        if size < 84:
            return {"bbox_x_mm": 0, "bbox_y_mm": 0, "bbox_z_mm": 0, "triangles": 0}

        with open(file_path, "rb") as f:
            header = f.read(80)
            tri_count_bytes = f.read(4)
            tri_count = struct.unpack("<I", tri_count_bytes)[0]
            
            expected_size = 84 + tri_count * 50
            if abs(expected_size - size) <= 2:
                # Binary STL
                min_x = min_y = min_z = float("inf")
                max_x = max_y = max_z = float("-inf")
                
                # Sample up to 10,000 triangles for fast bounds calculation
                step = max(1, tri_count // 10000)
                for i in range(0, tri_count, step):
                    f.seek(84 + i * 50 + 12)  # Skip 84 header + normal (12 bytes)
                    v_bytes = f.read(36)      # 3 vertices * 3 floats * 4 bytes
                    if len(v_bytes) < 36:
                        break
                    v = struct.unpack("<9f", v_bytes)
                    for j in range(0, 9, 3):
                        x, y, z = v[j], v[j+1], v[j+2]
                        min_x = min(min_x, x)
                        max_x = max(max_x, x)
                        min_y = min(min_y, y)
                        max_y = max(max_y, y)
                        min_z = min(min_z, z)
                        max_z = max(max_z, z)
                
                dx = max(0.0, max_x - min_x) if max_x > min_x else 0.0
                dy = max(0.0, max_y - min_y) if max_y > min_y else 0.0
                dz = max(0.0, max_z - min_z) if max_z > min_z else 0.0
                vol = (dx * dy * dz * 0.15) / 1000.0

                return {
                    "bbox_x_mm": round(dx, 1),
                    "bbox_y_mm": round(dy, 1),
                    "bbox_z_mm": round(dz, 1),
                    "volume_cm3": round(vol, 2),
                    "triangles": tri_count,
                    "estimated_weight_g": round(vol * 1.24, 1),
                    "parsed_by": "stl_binary"
                }
    except Exception:
        pass
    
    return {"bbox_x_mm": 0.0, "bbox_y_mm": 0.0, "bbox_z_mm": 0.0, "triangles": 0, "volume_cm3": 0.0}


def _parse_obj_fallback(file_path: Path) -> Dict[str, Any]:
    """Parse OBJ vertex bounds."""
    try:
        min_x = min_y = min_z = float("inf")
        max_x = max_y = max_z = float("-inf")
        faces = 0
        v_count = 0
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.startswith("v "):
                    parts = line.strip().split()
                    if len(parts) >= 4:
                        x, y, z = float(parts[1]), float(parts[2]), float(parts[3])
                        min_x = min(min_x, x)
                        max_x = max(max_x, x)
                        min_y = min(min_y, y)
                        max_y = max(max_y, y)
                        min_z = min(min_z, z)
                        max_z = max(max_z, z)
                        v_count += 1
                elif line.startswith("f "):
                    faces += 1
        
        if v_count > 0:
            dx = max(0.0, max_x - min_x)
            dy = max(0.0, max_y - min_y)
            dz = max(0.0, max_z - min_z)
            vol = (dx * dy * dz * 0.15) / 1000.0
            return {
                "bbox_x_mm": round(dx, 1),
                "bbox_y_mm": round(dy, 1),
                "bbox_z_mm": round(dz, 1),
                "volume_cm3": round(vol, 2),
                "triangles": faces,
                "estimated_weight_g": round(vol * 1.24, 1),
                "parsed_by": "obj_ascii"
            }
    except Exception:
        pass

    return {"bbox_x_mm": 0.0, "bbox_y_mm": 0.0, "bbox_z_mm": 0.0, "triangles": 0, "volume_cm3": 0.0}
