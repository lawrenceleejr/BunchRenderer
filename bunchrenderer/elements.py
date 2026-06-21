"""Parse beamline geometry used to decorate the real-space view.

Two formats are supported:

**VRML 1.0 (.wrl)** as exported by g4beamline itself: run g4bl with
``viewer=VRML1FILE`` and it writes ``g4_NN.wrl`` containing every visible
volume as a world-coordinate polyhedron in mm (Geant4 internal units) --
exactly matching BLTrackFile coordinates. We extract each shape's vertices,
faces/lines and material color.

**CSV** for hand-authored layouts, with header-based columns (lengths in mm):

    name,shape,z,length,rin,rout[,height][,x][,y]

* ``shape``  -- ``tube`` (hollow pipe: inner+outer wall), ``cylinder``
  (single outer wall) or ``box``
* ``z``      -- element center along the beamline [mm]
* ``length`` -- extent along z [mm]
* ``rin``    -- inner radius [mm] (tube only)
* ``rout``   -- outer radius [mm]; for ``box``: half-width in x
* ``height`` -- box only: half-height in y (defaults to ``rout``)
* ``x, y``   -- transverse offsets of the element center [mm], default 0
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

SHAPES = ("tube", "cylinder", "box")


class ElementsError(ValueError):
    """Raised when an elements file cannot be interpreted."""


# --------------------------------------------------------------------------
# VRML 1.0 (g4beamline `viewer=VRML1FILE` output)

def _vrml_tokens(text):
    # Drop comment lines (incl. the "#VRML V1.0 ascii" header itself), but
    # g4beamline records each volume's name only in a comment of the form
    #   #---------- SOLID: wedge0:0
    # (there is no DEF), so lift that name into a synthetic "__SOLID__ <name>"
    # token pair that load_vrml() can pick up like a DEF.
    lines = []
    for line in text.splitlines():
        code, _, comment = line.partition("#")
        m = re.search(r"SOLID:\s*(\S+)", comment)
        if m:
            code = f"__SOLID__ {m.group(1)} {code}"
        lines.append(code)
    return re.findall(r"[{}\[\]]|[^\s{}\[\],]+", "\n".join(lines))


def _collect_floats(tokens, i):
    """Read numbers from tokens[i:] until a closing bracket; returns (vals, i)."""
    vals = []
    while i < len(tokens) and tokens[i] not in ("]", "}"):
        try:
            vals.append(float(tokens[i]))
        except ValueError:
            pass  # stray keyword inside the list; ignore
        i += 1
    return vals, i


def load_vrml(path):
    """Extract shapes from a VRML 1.0 file written by Geant4/g4beamline.

    The G4 VRML1 driver emits, per visible volume, a Separator containing a
    Material, a Coordinate3 with *world*-frame points (mm), and an
    IndexedFaceSet (surfaces) or IndexedLineSet (wireframe). No nested
    transforms are used, so a flat scan with last-seen state is sufficient.
    Returns a list of mesh dicts: name, color, transparency, verts, faces,
    lines.
    """
    text = Path(path).read_text(errors="replace")
    if "#VRML" not in text.splitlines()[0] and "Coordinate3" not in text:
        raise ElementsError(f"{path}: does not look like a VRML 1.0 file")
    tokens = _vrml_tokens(text)

    meshes = []
    color, transparency = (0.4, 0.6, 0.9), 0.0
    verts = []
    name = ""
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("DEF", "__SOLID__") and i + 1 < len(tokens):
            name = tokens[i + 1]
            i += 2
            continue
        if tok == "diffuseColor":
            color = tuple(float(tokens[i + k]) for k in (1, 2, 3))
            i += 4
            continue
        if tok == "transparency":
            transparency = float(tokens[i + 1])
            i += 2
            continue
        if tok == "point":  # Coordinate3 { point [ x y z, ... ] }
            j = i + 1
            while j < len(tokens) and tokens[j] != "[":
                j += 1
            vals, j = _collect_floats(tokens, j + 1)
            verts = [tuple(vals[k:k + 3]) for k in range(0, len(vals) - 2, 3)]
            i = j + 1
            continue
        if tok == "coordIndex":  # IndexedFaceSet/IndexedLineSet polygons
            kind = "faces"
            for back in range(max(0, i - 12), i):
                if tokens[back] == "IndexedLineSet":
                    kind = "lines"
            j = i + 1
            while j < len(tokens) and tokens[j] != "[":
                j += 1
            vals, j = _collect_floats(tokens, j + 1)
            polys, cur = [], []
            for v in vals:
                if int(v) < 0:
                    if len(cur) >= 2:
                        polys.append(cur)
                    cur = []
                else:
                    cur.append(int(v))
            if len(cur) >= 2:
                polys.append(cur)
            if verts and polys:
                meshes.append({
                    "name": name,
                    "color": [round(c, 4) for c in color],
                    "transparency": round(transparency, 4),
                    "verts": [[round(c, 3) for c in v] for v in verts],
                    "faces": polys if kind == "faces" else [],
                    "lines": polys if kind == "lines" else [],
                })
            name = ""
            i = j + 1
            continue
        i += 1
    if not meshes:
        raise ElementsError(f"{path}: no IndexedFaceSet/IndexedLineSet shapes found")
    return meshes


# --------------------------------------------------------------------------
# Hand-authored CSV

def load_csv_elements(path):
    path = Path(path)
    with open(path, newline="") as fh:
        rows = [r for r in csv.reader(fh)
                if r and r[0].strip() and not r[0].lstrip().startswith("#")]
    if len(rows) < 2:
        raise ElementsError(f"{path}: need a header row and at least one element")

    names = [c.strip().lower() for c in rows[0]]
    required = ("name", "shape", "z", "length", "rout")
    missing = [c for c in required if c not in names]
    if missing:
        raise ElementsError(f"{path}: missing column(s): {', '.join(missing)}")
    idx = {n: i for i, n in enumerate(names)}

    def fget(row, col, default=0.0):
        i = idx.get(col)
        if i is None or i >= len(row) or not row[i].strip():
            return default
        return float(row[i])

    elements = []
    for n, row in enumerate(rows[1:], start=2):
        shape = row[idx["shape"]].strip().lower()
        if shape not in SHAPES:
            raise ElementsError(
                f"{path} line {n}: unknown shape {shape!r} (use {', '.join(SHAPES)})")
        rout = fget(row, "rout")
        if rout <= 0:
            raise ElementsError(f"{path} line {n}: rout must be positive")
        elements.append({
            "name": row[idx["name"]].strip(),
            "shape": shape,
            "z": fget(row, "z"),
            "length": fget(row, "length"),
            "rin": fget(row, "rin"),
            "rout": rout,
            "height": fget(row, "height", rout),
            "x": fget(row, "x"),
            "y": fget(row, "y"),
        })
    return elements


def load_elements(path):
    """Dispatch on file type; returns a JSON-serializable elements bundle."""
    path = Path(path)
    if not path.exists():
        raise ElementsError(f"elements file not found: {path}")
    first = ""
    try:
        with open(path, errors="replace") as fh:
            first = fh.readline()
    except OSError as exc:
        raise ElementsError(f"cannot read {path}: {exc}")
    if path.suffix.lower() in (".wrl", ".vrml") or first.startswith("#VRML"):
        return {"type": "vrml", "meshes": load_vrml(path)}
    return {"type": "csv", "items": load_csv_elements(path)}
