"""BunchRenderer scene builder. Runs *inside* Blender:

    blender -b --factory-startup --python scene_builder.py -- \
        --data data.json --output scene.blend [options]

Builds an animated scene from resampled track data (see bunchrenderer.tracks):

* "beam"  -- the bunch flying through real space, chased by a camera with
             motion blur, wrapped in an animated convex-hull envelope;
* phase-space stations -- 3D projections of phase space: every 3-subset of
             the 4D transverse phase space (x, x', y, y') plus longitudinal
             (dz, Pz, x) and (dz, Pz, y). Each station has labeled axes, a
             per-frame convex hull, and its own slowly orbiting camera so the
             evolution of the distribution reads clearly as a movie;
* "overview" -- a dashboard camera framing all phase-space stations.

Supports Blender 4.2 LTS through 5.x (tested on 4.5.10 and 5.0.1); >= 4.5
gives true Kelvin light temperatures. Version differences handled here:
slotted actions (5.0 removed Action.fcurves), the compositing node group
(5.0 removed Scene.node_tree), the socket-based Glare node, and video
output media types.
"""

import argparse
import json
import math
import os
import sys

import bpy
import bmesh
from mathutils import Matrix, Vector

FLOOR_Z = -2.6           # world height of the studio floor
STATION_HALF = 1.5       # data is normalized into a cube of this half-extent
STATION_SPACING = 14.0
STATION_COLS = 3
STATION_ORIGIN = (-STATION_SPACING, -30.0)  # x of first column, y of first row
BEAM_LENGTH = 28.0       # corridor length in blender units
BEAM_HALF_TRANSVERSE = 1.2   # target visual half-size of the bunch itself
BEAM_ORBIT_HALF = 4.0    # max visual excursion of the centroid orbit
BEAM_HEIGHT = 1.6

PALETTE = {
    "axis": (0.82, 0.84, 0.88),
    "text": (0.92, 0.94, 1.0),
}

# One color pair per beam (particles / soft-metallic hull tint), used
# consistently across the real-space view, stations and HUD panels.
BEAM_COLORS = [
    {"particle": (1.0, 0.62, 0.18), "hull": (0.72, 0.58, 0.42)},  # amber
    {"particle": (0.20, 0.85, 1.0), "hull": (0.45, 0.60, 0.75)},  # cyan
    {"particle": (0.45, 1.0, 0.45), "hull": (0.48, 0.68, 0.50)},  # green
    {"particle": (1.0, 0.45, 0.85), "hull": (0.70, 0.50, 0.65)},  # magenta
]


def parse_args():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else argv[1:]
    p = argparse.ArgumentParser(prog="scene_builder")
    p.add_argument("--data", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--frames", type=int, default=240)
    p.add_argument("--fps", type=int, default=24)
    p.add_argument("--samples", type=int, default=64)
    p.add_argument("--resolution", default="1920x1080")
    p.add_argument("--views", default="all")
    p.add_argument("--cameras", default="all")
    p.add_argument("--no-hull", action="store_true")
    p.add_argument("--no-hud", action="store_true")
    p.add_argument("--gpu", action="store_true")
    p.add_argument("--fade-in", type=float, default=0.75)
    p.add_argument("--hold", type=float, default=0.5)
    p.add_argument("--fade-out", type=float, default=1.5)
    p.add_argument("--render", action="store_true")
    p.add_argument("--render-dir", default="renders")
    p.add_argument("--format", choices=("mp4", "png", "both"), default="mp4")
    return p.parse_args(argv)


# --------------------------------------------------------------------------
# Small helpers (data-API only; no operators that need UI context)

# Bundled typeface (Lora, OFL) loaded by load_fonts(); falls back to
# Blender's built-in font when the files aren't found.
FONTS = {"regular": None, "bold": None}


def load_fonts():
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
    for key, fname in (("regular", "Lora-Regular.ttf"),
                       ("bold", "Lora-Bold.ttf")):
        path = os.path.join(base, fname)
        if not os.path.exists(path):
            continue
        try:
            font = bpy.data.fonts.load(path)
            try:
                font.pack()  # embed in the .blend so the file is portable
            except Exception:
                pass
            FONTS[key] = font
        except Exception as exc:
            print(f"[scene_builder] could not load font {fname}: {exc}")
    if FONTS["regular"] is None:
        print("[scene_builder] bundled fonts not found; using Blender's default")


def apply_font(curve, bold=False):
    font = FONTS["bold" if bold else "regular"] or FONTS["regular"]
    if font is not None:
        curve.font = font


def link(obj):
    bpy.context.scene.collection.objects.link(obj)
    return obj


def smooth_mesh(mesh):
    """Shade every face of a mesh smooth. Particle spheres (analytic Cycles
    points) and the convex hulls (Set Shade Smooth node) are already smooth;
    this covers the bmesh/from_pydata primitives -- arrows, pedestals, the
    floor and the beamline element solids."""
    n = len(mesh.polygons)
    if n:
        mesh.polygons.foreach_set("use_smooth", [True] * n)
        mesh.update()
    return mesh


def wipe_scene():
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)


def set_input(node, names, value):
    for name in names:
        sock = node.inputs.get(name)
        if sock is not None:
            try:
                sock.default_value = value
                return True
            except (TypeError, ValueError):
                pass
    return False


def make_material(name, base, metallic=0.0, roughness=0.5, alpha=1.0,
                  emission=None, emission_strength=0.0, coat=0.0):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = next(n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    set_input(bsdf, ("Base Color",), (*base, 1.0))
    set_input(bsdf, ("Metallic",), metallic)
    set_input(bsdf, ("Roughness",), roughness)
    set_input(bsdf, ("Alpha",), alpha)
    set_input(bsdf, ("Coat Weight", "Clearcoat"), coat)
    if emission is not None:
        set_input(bsdf, ("Emission Color", "Emission"), (*emission, 1.0))
        set_input(bsdf, ("Emission Strength",), emission_strength)
    if alpha < 1.0:
        try:  # EEVEE viewport preview niceness; Cycles ignores it
            mat.blend_method = "BLEND"
        except Exception:
            pass
    return mat


def _iface_socket(ng, name, in_out, socket_type):
    if hasattr(ng, "interface"):  # Blender 4.x
        ng.interface.new_socket(name=name, in_out=in_out, socket_type=socket_type)
    else:  # Blender 3.x
        coll = ng.inputs if in_out == "INPUT" else ng.outputs
        coll.new(socket_type, name)


def points_node_group(name, radius, material):
    """Geometry nodes: vertex cloud -> renderable points (spheres in Cycles)."""
    ng = bpy.data.node_groups.new(name, "GeometryNodeTree")
    _iface_socket(ng, "Geometry", "INPUT", "NodeSocketGeometry")
    _iface_socket(ng, "Geometry", "OUTPUT", "NodeSocketGeometry")
    nin = ng.nodes.new("NodeGroupInput")
    nout = ng.nodes.new("NodeGroupOutput")
    m2p = ng.nodes.new("GeometryNodeMeshToPoints")
    m2p.mode = "VERTICES"
    set_input(m2p, ("Radius",), radius)
    smat = ng.nodes.new("GeometryNodeSetMaterial")
    smat.inputs["Material"].default_value = material
    ng.links.new(nin.outputs[0], m2p.inputs["Mesh"])
    ng.links.new(m2p.outputs["Points"], smat.inputs["Geometry"])
    ng.links.new(smat.outputs["Geometry"], nout.inputs[0])
    return ng


def hull_node_group(name, material):
    """Geometry nodes: vertex cloud -> convex hull, recomputed every frame."""
    ng = bpy.data.node_groups.new(name, "GeometryNodeTree")
    _iface_socket(ng, "Geometry", "INPUT", "NodeSocketGeometry")
    _iface_socket(ng, "Geometry", "OUTPUT", "NodeSocketGeometry")
    nin = ng.nodes.new("NodeGroupInput")
    nout = ng.nodes.new("NodeGroupOutput")
    hull = ng.nodes.new("GeometryNodeConvexHull")
    smooth = ng.nodes.new("GeometryNodeSetShadeSmooth")
    smat = ng.nodes.new("GeometryNodeSetMaterial")
    smat.inputs["Material"].default_value = material
    ng.links.new(nin.outputs[0], hull.inputs["Geometry"])
    ng.links.new(hull.outputs["Convex Hull"], smooth.inputs["Geometry"])
    ng.links.new(smooth.outputs["Geometry"], smat.inputs["Geometry"])
    ng.links.new(smat.outputs["Geometry"], nout.inputs[0])
    return ng


def make_cloud(name, coords, sample_frames):
    """Mesh with one vertex per particle, animated through the time samples
    with absolute shape keys driven by an eval_time F-curve."""
    n_samples = len(coords)
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([tuple(v) for v in coords[0]], [], [])
    obj = bpy.data.objects.new(name, mesh)
    link(obj)

    obj.shape_key_add(name="s000", from_mix=False)
    for s in range(1, n_samples):
        kb = obj.shape_key_add(name=f"s{s:03d}", from_mix=False)
        flat = [c for v in coords[s] for c in v]
        kb.data.foreach_set("co", flat)

    key = mesh.shape_keys
    key.use_relative = False
    for kb in key.key_blocks:
        kb.interpolation = "KEY_LINEAR"
    for s, frame in enumerate(sample_frames):
        key.eval_time = key.key_blocks[s].frame
        key.keyframe_insert("eval_time", frame=frame)
    set_interpolation(key.animation_data, "LINEAR")
    return obj


def hull_source_coords(coords, spans):
    """Per-sample coords for the convex hull where particles that have stopped
    propagating are collapsed onto the centroid of the still-active particles,
    so they sit inside the hull and no longer inflate it. The visible point
    cloud keeps the real positions."""
    S, N = len(coords), len(coords[0])
    out = []
    for s in range(S):
        act = [i for i in range(N) if spans[i][0] <= s <= spans[i][1]]
        if act:
            cx = sum(coords[s][i][0] for i in act) / len(act)
            cy = sum(coords[s][i][1] for i in act) / len(act)
            cz = sum(coords[s][i][2] for i in act) / len(act)
            c, aset = (cx, cy, cz), set(act)
            out.append([coords[s][i] if i in aset else c for i in range(N)])
        else:
            out.append([coords[s][0]] * N)
    return out


def make_view_objects(name, coords, sample_frames, center, radius,
                      particle_mat, hull_mat, with_hull, spans=None):
    """Particle cloud + optional convex-hull envelope. The point cloud always
    shows every particle's real position; the hull is built from a separate
    mesh that drops particles which have stopped propagating (see
    hull_source_coords) when per-particle ``spans`` are supplied."""
    cloud = make_cloud(f"{name}_particles", coords, sample_frames)
    cloud.location = center
    mod = cloud.modifiers.new("Points", "NODES")
    mod.node_group = points_node_group(f"{name}_points", radius, particle_mat)
    try:
        # Keep particles as crisp dots: deformation motion blur would smear
        # each one along its (transversely exaggerated) velocity into a long
        # streak. Camera motion blur still conveys the forward motion.
        cloud.cycles.use_deform_motion = False
    except AttributeError:
        pass
    objs = [cloud]
    if with_hull:
        n_last = len(coords) - 1
        all_full = spans is None or all(s[0] <= 0 and s[1] >= n_last
                                        for s in spans)
        if all_full:
            hull = bpy.data.objects.new(f"{name}_hull", cloud.data)
            link(hull)
        else:
            hull = make_cloud(f"{name}_hull",
                              hull_source_coords(coords, spans), sample_frames)
        hull.location = center
        hmod = hull.modifiers.new("Hull", "NODES")
        hmod.node_group = hull_node_group(f"{name}_hullgn", hull_mat)
        try:
            # Hull topology changes between frames, which deformation motion
            # blur cannot handle; camera/object blur still applies.
            hull.cycles.use_deform_motion = False
        except AttributeError:
            pass
        objs.append(hull)
    return objs


def make_text(name, body, size, location, material, target=None,
              align_x="CENTER", extrude=0.0, parent=None, bold=False):
    cu = bpy.data.curves.new(name, "FONT")
    cu.body = body
    cu.size = size
    cu.align_x = align_x
    cu.align_y = "CENTER"
    cu.extrude = extrude
    apply_font(cu, bold)
    cu.materials.append(material)
    obj = bpy.data.objects.new(name, cu)
    if parent is not None:
        obj.parent = parent
    obj.location = location
    link(obj)
    if target is not None:
        con = obj.constraints.new("TRACK_TO")
        con.target = target
        con.track_axis = "TRACK_Z"   # text faces its target
        con.up_axis = "UP_Y"
    return obj


def make_arrow(name, origin, direction, length, radius, material, parent=None):
    d = Vector(direction).normalized()
    tip = min(0.24, length * 0.15)
    shaft = length - tip
    bm = bmesh.new()
    res = bmesh.ops.create_cone(bm, cap_ends=True, segments=20,
                                radius1=radius, radius2=radius, depth=shaft)
    bmesh.ops.translate(bm, verts=res["verts"], vec=(0, 0, shaft * 0.5))
    res = bmesh.ops.create_cone(bm, cap_ends=True, segments=20,
                                radius1=radius * 3.4, radius2=0.0, depth=tip)
    bmesh.ops.translate(bm, verts=res["verts"], vec=(0, 0, shaft + tip * 0.5))
    mesh = bpy.data.meshes.new(name)
    bm.to_mesh(mesh)
    bm.free()
    smooth_mesh(mesh)
    mesh.materials.append(material)
    obj = bpy.data.objects.new(name, mesh)
    if parent is not None:
        obj.parent = parent
    obj.location = origin
    obj.rotation_mode = "QUATERNION"
    obj.rotation_quaternion = Vector((0, 0, 1)).rotation_difference(d)
    return link(obj)


def make_empty(name, location):
    obj = bpy.data.objects.new(name, None)
    obj.location = location
    obj.empty_display_size = 0.2
    return link(obj)


def make_camera(name, location, target=None, lens=50.0):
    cd = bpy.data.cameras.new(name)
    cd.lens = lens
    cam = bpy.data.objects.new(name, cd)
    cam.location = location
    link(cam)
    if target is not None:
        con = cam.constraints.new("TRACK_TO")
        con.target = target
        con.track_axis = "TRACK_NEGATIVE_Z"
        con.up_axis = "UP_Y"
    return cam


def kelvin_to_rgb(kelvin):
    """Blackbody color fallback for Blender < 4.5 (no native light temperature)."""
    t = max(kelvin, 1000.0) / 100.0
    if t <= 66:
        r = 255.0
        g = 99.4708025861 * math.log(t) - 161.1195681661
        b = 0.0 if t <= 19 else 138.5177312231 * math.log(t - 10) - 305.0447927307
    else:
        r = 329.698727446 * ((t - 60) ** -0.1332047592)
        g = 288.1221695283 * ((t - 60) ** -0.0755148492)
        b = 255.0
    srgb = (min(max(c, 0.0), 255.0) / 255.0 for c in (r, g, b))
    return tuple(c ** 2.2 for c in srgb)  # rough sRGB -> linear


def enable_gpu():
    """Enable whichever Cycles GPU backend has devices (Metal on Apple
    Silicon, OPTIX/CUDA on NVIDIA, HIP on AMD, oneAPI on Intel)."""
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
    except KeyError:
        print("[scene_builder] Cycles preferences unavailable; using CPU")
        return False
    for dtype in ("OPTIX", "CUDA", "HIP", "METAL", "ONEAPI"):
        try:
            prefs.compute_device_type = dtype
        except TypeError:
            continue
        try:
            prefs.refresh_devices()
        except AttributeError:
            prefs.get_devices()
        gpus = [d for d in prefs.devices if d.type != "CPU"]
        if gpus:
            for d in prefs.devices:
                d.use = d.type != "CPU"
            print(f"[scene_builder] GPU rendering ({dtype}): "
                  + ", ".join(d.name for d in gpus))
            return True
    print("[scene_builder] --gpu requested but no GPU devices found; using CPU")
    return False


def make_area_light(name, location, target, size, power, temperature=4000.0):
    ld = bpy.data.lights.new(name, "AREA")
    ld.size = size
    ld.energy = power
    if hasattr(ld, "use_temperature"):  # Blender >= 4.5: real Kelvin lights
        ld.use_temperature = True
        ld.temperature = temperature
        ld.color = (1.0, 1.0, 1.0)
    else:
        ld.color = kelvin_to_rgb(temperature)
    obj = bpy.data.objects.new(name, ld)
    obj.location = location
    link(obj)
    con = obj.constraints.new("TRACK_TO")
    con.target = target
    con.track_axis = "TRACK_NEGATIVE_Z"
    con.up_axis = "UP_Y"
    return obj


def make_cylinder(name, location, radius, depth, material, segments=48):
    bm = bmesh.new()
    bmesh.ops.create_cone(bm, cap_ends=True, segments=segments,
                          radius1=radius, radius2=radius, depth=depth)
    mesh = bpy.data.meshes.new(name)
    bm.to_mesh(mesh)
    bm.free()
    smooth_mesh(mesh)
    mesh.materials.append(material)
    obj = bpy.data.objects.new(name, mesh)
    obj.location = location
    return link(obj)


def make_plane(name, location, size, material):
    mesh = bpy.data.meshes.new(name)
    h = size / 2.0
    mesh.from_pydata([(-h, -h, 0), (h, -h, 0), (h, h, 0), (-h, h, 0)],
                     [], [(0, 1, 2, 3)])
    smooth_mesh(mesh)
    mesh.materials.append(material)
    obj = bpy.data.objects.new(name, mesh)
    obj.location = location
    return link(obj)


def anim_fcurves(animdata):
    """All F-curves of a datablock's action, across Blender versions.

    Blender <= 4.x exposes ``action.fcurves`` directly; Blender 5.0 removed
    that legacy API in favor of slotted actions (layers > strips >
    channelbags)."""
    if animdata is None or animdata.action is None:
        return []
    action = animdata.action
    if hasattr(action, "fcurves"):
        return list(action.fcurves)
    slot = getattr(animdata, "action_slot", None)
    out = []
    for layer in action.layers:
        for strip in layer.strips:
            for bag in getattr(strip, "channelbags", []):
                if slot is None or bag.slot_handle == slot.handle:
                    out.extend(bag.fcurves)
    return out


def set_interpolation(animdata, mode):
    for fc in anim_fcurves(animdata):
        for kp in fc.keyframe_points:
            kp.interpolation = mode


def move_to_collection(obj, col):
    for c in list(obj.users_collection):
        c.objects.unlink(obj)
    col.objects.link(obj)
    return obj


def overlay_only(obj):
    """Camera overlay objects: visible to the camera, invisible to shadows
    and secondary rays so they never affect the scene's lighting."""
    for prop in ("visible_shadow", "visible_diffuse", "visible_glossy",
                 "visible_transmission", "visible_volume_scatter"):
        if hasattr(obj, prop):
            setattr(obj, prop, False)
    return obj


def make_frame_rect(name, half_w, half_h, thickness, material):
    """Thin rectangular border (curve with bevel) for HUD panels."""
    cu = bpy.data.curves.new(name, "CURVE")
    cu.dimensions = "3D"
    cu.bevel_depth = thickness
    cu.materials.append(material)
    sp = cu.splines.new("POLY")
    sp.points.add(3)
    for pt, (x, y) in zip(sp.points, ((-half_w, -half_h), (half_w, -half_h),
                                      (half_w, half_h), (-half_w, half_h))):
        pt.co = (x, y, 0.0, 1.0)
    sp.use_cyclic_u = True
    return bpy.data.objects.new(name, cu)


def fmt(v):
    return f"{v:+.3g}" if abs(v) >= 1e-12 else "0"


# --------------------------------------------------------------------------
# Phase-space channels

def combined_centroids(beams):
    """Per-sample centroid over the particles of *all* beams: [S][3] in mm."""
    S = len(beams[0]["pos"])
    out = []
    for s in range(S):
        acc, n = [0.0, 0.0, 0.0], 0
        for beam in beams:
            for p in beam["pos"][s]:
                acc[0] += p[0]
                acc[1] += p[1]
                acc[2] += p[2]
                n += 1
        out.append([a / n for a in acc])
    return out


def compute_channels(beam, centroids):
    """Per-sample, per-particle phase-space coordinates for one beam.

    dz is measured against the combined multi-beam centroid so that a
    longitudinal offset between beams stays visible.
    """
    pos, mom = beam["pos"], beam["mom"]
    S, N = len(pos), len(pos[0])
    ch = {k: [[0.0] * N for _ in range(S)] for k in
          ("x", "y", "xp", "yp", "dz", "pz")}
    for s in range(S):
        zc = centroids[s][2]
        for i in range(N):
            x, y, z = pos[s][i]
            px, py, pz = mom[s][i]
            safe_pz = pz if abs(pz) > 1e-12 else 1e-12
            ch["x"][s][i] = x
            ch["y"][s][i] = y
            ch["xp"][s][i] = 1000.0 * px / safe_pz   # mrad
            ch["yp"][s][i] = 1000.0 * py / safe_pz
            ch["dz"][s][i] = z - zc                  # mm, relative to centroid
            ch["pz"][s][i] = pz                      # MeV/c
    return ch


# (channel, axis label) per axis. All views are 3D projections; the four
# transverse ones cover every 3-subset of the 4D phase space (x, x', y, y').
VIEW_DEFS = {
    "xxpy":  {"axes": [("x", "x [mm]"), ("xp", "x′ [mrad]"), ("y", "y [mm]")],
              "title": "4D phase space — (x, x′, y)"},
    "xxpyp": {"axes": [("x", "x [mm]"), ("xp", "x′ [mrad]"), ("yp", "y′ [mrad]")],
              "title": "4D phase space — (x, x′, y′)"},
    "xyyp":  {"axes": [("x", "x [mm]"), ("y", "y [mm]"), ("yp", "y′ [mrad]")],
              "title": "4D phase space — (x, y, y′)"},
    "xpyyp": {"axes": [("xp", "x′ [mrad]"), ("y", "y [mm]"), ("yp", "y′ [mrad]")],
              "title": "4D phase space — (x′, y, y′)"},
    "zpzx":  {"axes": [("dz", "Δz [mm]"), ("pz", "Pz [MeV/c]"), ("x", "x [mm]")],
              "title": "Longitudinal — (Δz, Pz, x)"},
    "zpzy":  {"axes": [("dz", "Δz [mm]"), ("pz", "Pz [MeV/c]"), ("y", "y [mm]")],
              "title": "Longitudinal — (Δz, Pz, y)"},
}
VIEW_ORDER = ["xxpy", "xxpyp", "xyyp", "xpyyp", "zpzx", "zpzy"]


def axis_norm(channel_data_per_beam):
    """Symmetric center/scale fitting *all* beams in the station cube, so a
    station's axes are shared and the beams are directly comparable."""
    lo = min(min(min(row) for row in ch) for ch in channel_data_per_beam)
    hi = max(max(max(row) for row in ch) for ch in channel_data_per_beam)
    center = 0.5 * (lo + hi)
    half = max(0.5 * (hi - lo), 1e-9)
    scale = STATION_HALF / (half * 1.05)
    return center, half, scale


# --------------------------------------------------------------------------
# Scene assembly

class Builder:
    def __init__(self, args):
        self.args = args
        with open(args.data) as fh:
            self.data = json.load(fh)
        self.beams = self.data["beams"]
        self.n_beams = len(self.beams)
        self.S = self.data["meta"]["n_samples"]
        self.centroids = combined_centroids(self.beams)  # [S][3] mm

        # Timeline: lights ramp on, the beam evolves, the final state holds
        # (cameras keep orbiting), then the lights fade to black.
        self.fade_in_f = max(0, int(round(args.fade_in * args.fps)))
        self.hold_f = max(0, int(round(args.hold * args.fps)))
        self.fade_out_f = max(0, int(round(args.fade_out * args.fps)))
        self.evo_frames = args.frames
        self.total_frames = (self.fade_in_f + self.evo_frames
                             + self.hold_f + self.fade_out_f)
        f0 = 1 + self.fade_in_f
        self.sample_frames = [f0 + (self.evo_frames - 1) * s / (self.S - 1)
                              for s in range(self.S)]
        # Bunch centroid z [mm] per sample, for the HUD z-readouts.
        self.cz = [c[2] for c in self.centroids]

        self.cameras = {}
        self.beam_cols = []
        self.overlays = {}      # camera label -> {"scene", "lens"}
        self.comp_ov_rl = None  # compositor RLayers node showing the overlay
        w, _, h = args.resolution.lower().partition("x")
        self.aspect = int(h or 1080) / int(w or 1920)
        self.mats = {}
        self.lights = []
        self.hud_cols = {}
        self.with_hull = not args.no_hull
        self.with_hud = not args.no_hud
        if args.views == "all":
            self.views = ["beam"] + VIEW_ORDER
        else:
            self.views = [v for v in args.views.split(",") if v]

    # -- global setup ------------------------------------------------------

    def setup_scene(self):
        scene = bpy.context.scene
        wipe_scene()
        scene.render.engine = "CYCLES"
        scene.cycles.samples = self.args.samples
        scene.cycles.use_denoising = True
        scene.cycles.device = "GPU" if self.args.gpu and enable_gpu() else "CPU"
        # Plenty of transparent bounces so the beam stays visible even through
        # many layers of low-opacity beamline geometry.
        scene.cycles.transparent_max_bounces = 128
        scene.render.use_motion_blur = True
        scene.render.motion_blur_shutter = 0.55
        scene.render.fps = self.args.fps
        scene.frame_start = 1
        scene.frame_end = self.total_frames
        w, _, h = self.args.resolution.lower().partition("x")
        scene.render.resolution_x = int(w or 1920)
        scene.render.resolution_y = int(h or 1080)
        scene.render.resolution_percentage = 100
        for look in ("AgX - Medium High Contrast", "Medium High Contrast"):
            try:
                scene.view_settings.look = look
                break
            except TypeError:
                continue

        world = bpy.data.worlds.new("BunchWorld")
        scene.world = world
        world.use_nodes = True
        bg = world.node_tree.nodes.get("Background")
        if bg:
            bg.inputs[0].default_value = (0.004, 0.005, 0.009, 1.0)
            bg.inputs[1].default_value = 1.0

        try:  # subtle glow on emissive particles
            if hasattr(scene, "node_tree"):  # Blender <= 4.x
                scene.use_nodes = True
                tree = scene.node_tree
                tree.nodes.clear()
                out_node = tree.nodes.new("CompositorNodeComposite")
            else:  # Blender >= 5.0: compositing node group datablock
                tree = bpy.data.node_groups.new("BunchCompositing",
                                                "CompositorNodeTree")
                _iface_socket(tree, "Image", "OUTPUT", "NodeSocketColor")
                out_node = tree.nodes.new("NodeGroupOutput")
                scene.compositing_node_group = tree
            rl = tree.nodes.new("CompositorNodeRLayers")
            glare = tree.nodes.new("CompositorNodeGlare")
            if hasattr(glare, "glare_type"):  # <= 4.x property
                glare.glare_type = "FOG_GLOW"
            else:  # 5.x menu socket
                glare.inputs["Type"].default_value = "Fog Glow"
            if glare.inputs.get("Strength") is not None:  # >= 4.4 sockets
                glare.inputs["Threshold"].default_value = 1.0
                glare.inputs["Strength"].default_value = 0.18
                glare.inputs["Size"].default_value = 0.6
            else:  # <= 4.3 node properties
                glare.quality = "MEDIUM"
                glare.threshold = 1.0
                glare.size = 8
                glare.mix = -0.7
            # Flat text overlays render in a separate ortho scene and are
            # alpha-composited over each frame, after the glare pass so the
            # text stays pixel-crisp. The overlay RLayers node is pointed at
            # the active camera's OV_* scene (see set_active_hud).
            alpha = tree.nodes.new("CompositorNodeAlphaOver")
            ov_rl = tree.nodes.new("CompositorNodeRLayers")
            rgba = [s for s in alpha.inputs if s.type == "RGBA"]
            tree.links.new(rl.outputs["Image"], glare.inputs["Image"])
            tree.links.new(glare.outputs["Image"], rgba[0])
            tree.links.new(ov_rl.outputs["Image"], rgba[1])
            tree.links.new(alpha.outputs["Image"], out_node.inputs[0])
            self.comp_ov_rl = ov_rl
        except Exception as exc:
            print(f"[scene_builder] compositor glare skipped: {exc}")

    def setup_materials(self):
        for b in range(self.n_beams):
            c = BEAM_COLORS[b % len(BEAM_COLORS)]
            self.mats[f"particle{b}"] = make_material(
                f"BR_Particle{b}", c["particle"], metallic=0.6, roughness=0.3,
                emission=c["particle"], emission_strength=2.5)
            self.mats[f"hull{b}"] = make_material(
                f"BR_Hull{b}", c["hull"], metallic=1.0, roughness=0.32,
                alpha=0.42, coat=0.25)
            self.mats[f"hud_particle{b}"] = make_material(
                f"BR_HudParticle{b}", c["particle"],
                emission=c["particle"], emission_strength=5.0)
            self.mats[f"hud_hull{b}"] = make_material(
                f"BR_HudHull{b}", c["hull"], alpha=0.20,
                emission=c["hull"], emission_strength=0.5)
            self.mats[f"legend{b}"] = make_material(
                f"BR_Legend{b}", c["particle"],
                emission=c["particle"], emission_strength=2.0)
        self.mats["axis"] = make_material(
            "BR_Axis", PALETTE["axis"], metallic=0.9, roughness=0.35)
        self.mats["text"] = make_material(
            "BR_Text", PALETTE["text"], metallic=0.0, roughness=0.5,
            emission=PALETTE["text"], emission_strength=1.2)
        self.mats["floor"] = make_material(
            "BR_Floor", (0.030, 0.032, 0.038), metallic=0.85, roughness=0.28)
        self.mats["pedestal"] = make_material(
            "BR_Pedestal", (0.06, 0.065, 0.075), metallic=0.7, roughness=0.4)
        # Flat composited text overlays (rendered unlit in the OV_* scenes,
        # so pure emission sets the brightness).
        self.mats["ov_text"] = make_material(
            "BR_OvText", (1.0, 1.0, 1.0), emission=(1.0, 1.0, 1.0),
            emission_strength=5.0)
        for b in range(self.n_beams):
            c = BEAM_COLORS[b % len(BEAM_COLORS)]
            # Lower strength than the white text keeps the hue from clipping
            # to white through the AgX view transform.
            self.mats[f"ov_legend{b}"] = make_material(
                f"BR_OvLegend{b}", c["particle"], emission=c["particle"],
                emission_strength=1.8)
        # Camera-locked HUD panel furniture (warm neutral, understated).
        self.mats["hud_frame"] = make_material(
            "BR_HudFrame", (0.78, 0.72, 0.58), emission=(0.78, 0.72, 0.58),
            emission_strength=1.0)
        self.mats["hud_backdrop"] = make_material(
            "BR_HudBackdrop", (0.005, 0.006, 0.01), roughness=1.0, alpha=0.6)
        # Beamline elements: ghostly solid + thin, faint emissive wireframe.
        self.mats["elem_wire"] = make_material(
            "BR_ElemWire", (0.25, 0.8, 1.0), emission=(0.25, 0.8, 1.0),
            emission_strength=0.11, alpha=0.025)
        self.mats["elem_label"] = make_material(
            "BR_ElemLabel", (0.55, 0.85, 1.0), emission=(0.55, 0.85, 1.0),
            emission_strength=1.0)
        self._elem_solid_cache = {}

    def elem_solid_material(self, color):
        """Non-occluding ghost material for beamline elements: a pure
        Transparent BSDF (passes 100% of whatever is behind it, with no
        Fresnel/specular -- so even a bore wall seen edge-on never hides the
        beam) plus a faint additive Emission shell tinted by the geometry's
        own color. It can only *add* a soft glow, never darken or block."""
        key = tuple(round(c, 3) for c in color)
        mat = self._elem_solid_cache.get(key)
        if mat is not None:
            return mat
        mat = bpy.data.materials.new(f"BR_ElemSolid_{len(self._elem_solid_cache)}")
        mat.use_nodes = True
        nt = mat.node_tree
        nt.nodes.clear()
        out = nt.nodes.new("ShaderNodeOutputMaterial")
        add = nt.nodes.new("ShaderNodeAddShader")
        transp = nt.nodes.new("ShaderNodeBsdfTransparent")
        emit = nt.nodes.new("ShaderNodeEmission")
        emit.inputs["Color"].default_value = (*key, 1.0)
        emit.inputs["Strength"].default_value = 0.01  # very faint shell
        nt.links.new(emit.outputs[0], add.inputs[0])
        nt.links.new(transp.outputs[0], add.inputs[1])
        nt.links.new(add.outputs[0], out.inputs["Surface"])
        self._elem_solid_cache[key] = mat
        return mat

    # -- beam (real space) view ---------------------------------------------

    def build_beam_view(self):
        S = self.S
        cents = self.centroids
        zs = [p[2] for beam in self.beams for row in beam["pos"] for p in row]
        zmin, zmax = min(zs), max(zs)
        s_long = BEAM_LENGTH / max(zmax - zmin, 1e-9)

        # Decompose transverse motion: "spread" is each bunch's size around
        # its own traveling centroid; "orbit" is how far any beam's centroid
        # strays from the static reference (off-axis trajectories, inter-beam
        # separation -- whatever the data contains; nothing is imposed). One
        # uniform transverse scale keeps geometry faithful: the bunch targets
        # BEAM_HALF_TRANSVERSE units, the excursion may use up to
        # BEAM_ORBIT_HALF.
        cx0 = sum(c[0] for c in cents) / S
        cy0 = sum(c[1] for c in cents) / S
        beam_cents = []  # per-beam centroid path [S][2] (transverse, mm)
        spread, orbit, sep = 1e-9, 1e-9, 0.0
        for beam in self.beams:
            bc = []
            for s, row in enumerate(beam["pos"]):
                n = len(row)
                bx = sum(p[0] for p in row) / n
                by = sum(p[1] for p in row) / n
                bc.append((bx, by))
                orbit = max(orbit, abs(bx - cx0), abs(by - cy0))
                sep = max(sep, abs(bx - cents[s][0]), abs(by - cents[s][1]))
                for p in row:
                    spread = max(spread, abs(p[0] - bx), abs(p[1] - by))
            beam_cents.append(bc)
        s_trans = min(BEAM_HALF_TRANSVERSE / spread, BEAM_ORBIT_HALF / orbit)
        exag = s_trans / s_long
        ext_t = (orbit + spread) * s_trans
        height = max(BEAM_HEIGHT, FLOOR_Z + 1.2 + ext_t)

        def to_world(p):
            return ((p[0] - cx0) * s_trans,
                    (p[2] - zmin) * s_long,
                    height + (p[1] - cy0) * s_trans)

        # Camera rig: an empty dollies *straight* down the beamline, keeping
        # pace with the leading particle (the one furthest along z), with its
        # transverse position pinned to the corridor center. The camera is
        # parented to it and tracks it, so the beam's transverse motion shows
        # as the bunch moving within the frame and the camera never corkscrews.
        # Motion blur still comes for free from the forward dolly.
        lead_y = []
        for s in range(S):
            zmax_s = max(p[2] for beam in self.beams for p in beam["pos"][s])
            lead_y.append((zmax_s - zmin) * s_long)
        tpath = [Vector((0.0, lead_y[s], height)) for s in range(S)]
        target = make_empty("BeamTarget", tpath[0])
        for s, frame in enumerate(self.sample_frames):
            target.location = tpath[s]
            target.keyframe_insert("location", frame=frame)
        set_interpolation(target.animation_data, "LINEAR")

        cam = make_camera("Cam_beam", (0, 0, 0), target=target, lens=46)
        cam.parent = target
        # Half-coverage of the base framing (lens 46, offset 10.3) is ~2.27
        # units at the target plane; scale the offset so the farthest bunch
        # edge stays in frame even though the camera stays on the axis.
        dmax = (sep + spread) * s_trans
        off = min(2.5, max(0.55, 1.2 * dmax / 2.27))
        # Base offset chosen to stay outside typical beam-pipe ghost geometry.
        cam.location = (-5.4 * off, -8.2 * off, 3.2 * off)
        cam.data.dof.use_dof = True
        cam.data.dof.focus_object = target
        cam.data.dof.aperture_fstop = 4.0
        self.cameras["beam"] = cam

        for b, beam in enumerate(self.beams):
            bcol = self.beam_collection(b)
            coords = [[to_world(p) for p in row] for row in beam["pos"]]
            objs = make_view_objects(
                f"beam_b{b}", coords, self.sample_frames, (0, 0, 0),
                radius=0.01125, particle_mat=self.mats[f"particle{b}"],
                hull_mat=self.mats[f"hull{b}"], with_hull=self.with_hull,
                spans=beam["span"])
            for obj in objs:
                move_to_collection(obj, bcol)

        # Labeled axis tripod, pinned to a fixed lower-left spot in the
        # camera's view so it is always fully framed (the camera's world
        # orientation is constant during the flight, so placing the gizmo at a
        # fixed camera-relative offset keeps it on-screen at constant size;
        # parenting the arrows to the rotation-free target keeps them aligned
        # to the world x/y/z axes).
        axmat, txmat = self.mats["axis"], self.mats["text"]
        cam_off = Vector((-5.4, -8.2, 3.2)) * off
        cam_rot = (-cam_off).to_track_quat("-Z", "Y")
        gizmo = cam_off + cam_rot @ Vector((-0.92, -0.52, -3.5))
        alen = 0.45
        make_arrow("beam_ax_x", gizmo, (1, 0, 0), alen, 0.012, axmat, parent=target)
        make_arrow("beam_ax_y", gizmo, (0, 0, 1), alen, 0.012, axmat, parent=target)
        make_arrow("beam_ax_zdir", gizmo, (0, 1, 0), alen, 0.012, axmat, parent=target)
        make_text("beam_lbl_x", "x", 0.16, gizmo + Vector((alen + 0.14, 0, 0)),
                  txmat, target=cam, parent=target)
        make_text("beam_lbl_y", "y", 0.16, gizmo + Vector((0, 0, alen + 0.14)),
                  txmat, target=cam, parent=target)
        make_text("beam_lbl_zdir", "z", 0.16, gizmo + Vector((0, alen + 0.14, 0)),
                  txmat, target=cam, parent=target)

        # Title block as a flat composited overlay (fixed in frame).
        self.hud_collection("beam")
        self.make_overlay("beam", lens=46.0)
        self.add_hud_text("beam", "beam_title", "Real space — beam frame",
                          0.15, 0.0, 0.80, 4.5, bold=True)
        self.add_hud_text("beam", "beam_caption",
                          f"transverse scale ×{exag:.0f}", 0.085, 0.0, 0.655, 4.5)
        self.add_z_readout("beam", "beam", 0.085, 0.0, 0.55, 4.5)
        self.add_legend("beam", "beam", -1.62, 0.82, 4.5, 0.105)

        # Straight reference axis at the nominal beamline (x = y = 0); off-axis
        # orbits sweep around it. The current z is shown by the HUD readout, so
        # no in-scene z-tick labels (which the moving camera would smear).
        origin = Vector(to_world((0.0, 0.0, zmin)))
        make_arrow("beam_ax_z", origin, (0, 1, 0), BEAM_LENGTH + 1.5, 0.016, axmat)

        # Corridor lighting: warm key lights along the flight path with a
        # slightly cooler (but still warm) rim from the side.
        for k, yfrac in enumerate((0.15, 0.5, 0.85)):
            loc = (2.5, BEAM_LENGTH * yfrac, height + 8.0)
            self.lights.append(make_area_light(
                f"BeamKey_{k}", loc, target, size=9.0 + ext_t, power=3000,
                temperature=3100.0))
        self.lights.append(make_area_light(
            "BeamRim", (-6.0 - ext_t, BEAM_LENGTH * 0.7, height + 1.5),
            target, size=12.0, power=1500, temperature=3900.0))

        self.build_elements(to_world, s_trans, s_long, cam)

    # -- camera-locked HUD overlays -------------------------------------------

    def hud_collection(self, label):
        col = bpy.data.collections.new(f"HUD_{label}")
        bpy.context.scene.collection.children.link(col)
        self.hud_cols[label] = col
        return col

    def beam_collection(self, b):
        """Per-beam collection so each input's objects can be toggled."""
        while len(self.beam_cols) <= b:
            i = len(self.beam_cols)
            col = bpy.data.collections.new(f"Beam{i}_{self.beams[i]['label']}")
            bpy.context.scene.collection.children.link(col)
            self.beam_cols.append(col)
        return self.beam_cols[b]

    def add_legend(self, label, prefix, x, y, depth, size):
        """Color-coded beam labels, fixed to the top-left of the frame."""
        if self.n_beams < 2:
            return
        for b, beam in enumerate(self.beams):
            self.add_hud_text(label, f"{prefix}_legend{b}", beam["label"],
                              size, x, y - b * size * 1.6, depth,
                              material=self.mats[f"ov_legend{b}"], align_x="LEFT")

    def make_overlay(self, label, lens):
        """A 2D overlay scene for one camera: flat text in front of an ortho
        camera, alpha-composited over the render. No depth, no perspective,
        no motion blur, no lighting."""
        ov = bpy.data.scenes.new(f"OV_{label}")
        ov.render.engine = "CYCLES"
        # Text edges are resolved by anti-aliasing samples; too few makes the
        # glyph outlines noisy ("hairy"). The layer is mostly empty, so with
        # adaptive sampling this stays cheap.
        ov.cycles.samples = 64
        try:
            ov.cycles.use_adaptive_sampling = True
            ov.cycles.use_denoising = False
        except AttributeError:
            pass
        # A narrower pixel filter keeps the glyph edges crisp (the default
        # 1.5 px reconstruction filter visibly softens small text).
        ov.render.filter_size = 1.0
        ov.render.film_transparent = True
        ov.render.use_motion_blur = False
        try:
            ov.cycles.device = bpy.context.scene.cycles.device
        except AttributeError:
            pass
        ov.render.resolution_x = bpy.context.scene.render.resolution_x
        ov.render.resolution_y = bpy.context.scene.render.resolution_y
        ov.render.fps = self.args.fps
        ov.frame_start, ov.frame_end = 1, self.total_frames
        camd = bpy.data.cameras.new(f"OVCam_{label}")
        camd.type = "ORTHO"
        camd.ortho_scale = 2.0  # overlay coords: x in [-1, 1], y in [-a, a]
        cam = bpy.data.objects.new(f"OVCam_{label}", camd)
        cam.location = (0.0, 0.0, 5.0)
        ov.collection.objects.link(cam)
        ov.camera = cam
        self.overlays[label] = {"scene": ov, "lens": lens}
        return ov

    def add_hud_text(self, label, name, body, size, x, y, depth,
                     material=None, align_x="CENTER", bold=False):
        """Flat overlay text at the frame position where a 3D object at
        camera-space (x, y, -depth) would appear (keeps historical layout
        numbers), rendered as a 2D composite on top of the frame."""
        ov = self.overlays[label]
        half_w = depth * 18.0 / ov["lens"]
        half_h = half_w * self.aspect
        cu = bpy.data.curves.new(name, "FONT")
        cu.body = body
        cu.size = size / half_w
        cu.align_x = align_x
        cu.align_y = "CENTER"
        cu.resolution_u = 16  # smoother glyph outlines than the default 12
        apply_font(cu, bold)
        cu.materials.append(material or self.mats["ov_text"])
        obj = bpy.data.objects.new(name, cu)
        obj.location = (x / half_w, y / half_h * self.aspect, 0.0)
        ov["scene"].collection.objects.link(obj)
        return obj

    def add_z_readout(self, label, prefix, size, x, y, depth, step=None):
        """Animated 'z = ... m' readout: a sequence of camera-locked texts
        with stepped visibility keyframes (text bodies are not animatable).
        The update step scales with the timeline so long movies don't spawn
        thousands of text objects (~120 segments per camera at most)."""
        if step is None:
            step = max(8, self.total_frames // 120)
        f0 = 1 + self.fade_in_f

        def z_at(frame):
            if self.evo_frames <= 1 or self.S <= 1:
                s = 0
            else:
                t = (frame - f0) * (self.S - 1) / (self.evo_frames - 1)
                s = min(max(int(round(t)), 0), self.S - 1)
            return self.cz[s] / 1000.0  # meters

        segments, start = [], 1
        current = f"z = {z_at(1):.2f} m"
        for f in range(1 + step, self.total_frames + 1, step):
            txt = f"z = {z_at(f):.2f} m"
            if txt != current:
                segments.append((start, f - 1, current))
                start, current = f, txt
        segments.append((start, self.total_frames, current))

        for i, (fa, fb, body) in enumerate(segments):
            t = self.add_hud_text(label, f"{prefix}_zread{i:03d}", body,
                                  size, x, y, depth)
            if len(segments) == 1:
                continue
            for prop in ("hide_render", "hide_viewport"):
                if fa > 1:
                    setattr(t, prop, True)
                    t.keyframe_insert(prop, frame=1)
                setattr(t, prop, False)
                t.keyframe_insert(prop, frame=fa)
                if fb < self.total_frames:
                    setattr(t, prop, True)
                    t.keyframe_insert(prop, frame=fb + 1)
            set_interpolation(t.animation_data, "CONSTANT")

    def build_station_hud(self, vid, cam, axes, beam_coords):
        """Heads-up display for a 3D station camera: title, z-readout, beam
        legend, and the three 2D sub-projections of this view as small panels
        on the right (every beam overlaid in its own color)."""
        col = self.hud_collection(vid)
        self.make_overlay(vid, lens=40.0)
        self.add_hud_text(vid, f"{vid}_title", VIEW_DEFS[vid]["title"],
                          0.105, 0.0, 0.62, 3.0, bold=True)
        self.add_z_readout(vid, vid, 0.075, 0.0, 0.52, 3.0)
        self.add_legend(vid, vid, -1.28, 0.64, 3.0, 0.07)
        if not self.with_hud:
            return

        # Panel column layout: pitch leaves clear air between each panel's
        # caption and the frame of the panel below it.
        depth, px, half, margin = 3.0, 1.02, 0.14, 0.04
        ys = (0.48, 0.0, -0.48)
        short = [label.split(" [")[0] for _, label in axes]
        for pi, (ia, ib) in enumerate(((0, 1), (0, 2), (1, 2))):
            pos = (px, ys[pi], -depth)
            scale = half * 0.95 / STATION_HALF
            panel_objs = []
            for b, coords in enumerate(beam_coords):
                spans = self.beams[b]["span"]
                pcoords = [[(v[ia] * scale, v[ib] * scale, 0.0) for v in row]
                           for row in coords]
                cloud = make_cloud(f"{vid}_hud{pi}_b{b}", pcoords,
                                   self.sample_frames)
                cloud.parent = cam
                cloud.location = pos
                mod = cloud.modifiers.new("Points", "NODES")
                mod.node_group = points_node_group(
                    f"{vid}_hud{pi}_b{b}_pts", 0.0016,
                    self.mats[f"hud_particle{b}"])
                try:
                    cloud.cycles.use_deform_motion = False
                except AttributeError:
                    pass
                n_last = len(pcoords) - 1
                if all(s[0] <= 0 and s[1] >= n_last for s in spans):
                    hull = bpy.data.objects.new(f"{vid}_hud{pi}_b{b}_hull",
                                                cloud.data)
                    link(hull)
                else:
                    hull = make_cloud(f"{vid}_hud{pi}_b{b}_hull",
                                      hull_source_coords(pcoords, spans),
                                      self.sample_frames)
                hull.parent = cam
                hull.location = pos
                hmod = hull.modifiers.new("Hull", "NODES")
                hmod.node_group = hull_node_group(
                    f"{vid}_hud{pi}_b{b}_hullgn", self.mats[f"hud_hull{b}"])
                try:
                    hull.cycles.use_deform_motion = False
                except AttributeError:
                    pass
                panel_objs += [cloud, hull]
            frame = make_frame_rect(f"{vid}_hud{pi}_frame", half + margin,
                                    half + margin, 0.0035, self.mats["hud_frame"])
            link(frame)
            frame.parent = cam
            frame.location = pos
            back = make_plane(f"{vid}_hud{pi}_back", (0, 0, 0),
                              2 * (half + margin), self.mats["hud_backdrop"])
            back.parent = cam
            back.location = (px, ys[pi], -depth - 0.015)
            self.add_hud_text(
                vid, f"{vid}_hud{pi}_cap", f"{short[ia]} – {short[ib]}",
                0.05, px, ys[pi] - half - margin - 0.055, depth)
            for obj in panel_objs + [frame, back]:
                overlay_only(obj)
                move_to_collection(obj, col)

    # -- beamline elements (real-space view decoration) -----------------------

    def build_elements(self, to_world, s_trans, s_long, cam):
        els = self.data.get("elements")
        if not els:
            return
        if els.get("type") == "vrml":
            self._build_vrml_elements(els["meshes"], to_world)
        else:
            self._build_csv_elements(els["items"], to_world, s_trans, s_long, cam)

    def _ghost_pair(self, name, mesh, color):
        """Solid translucent object + brighter wireframe overlay (tron look)."""
        smooth_mesh(mesh)
        solid = bpy.data.objects.new(name, mesh)
        mesh.materials.append(self.elem_solid_material(color))
        link(solid)
        wire_mesh = mesh.copy()
        wire_mesh.materials.clear()
        wire_mesh.materials.append(self.mats["elem_wire"])
        wire = bpy.data.objects.new(f"{name}_wire", wire_mesh)
        link(wire)
        mod = wire.modifiers.new("Wireframe", "WIREFRAME")
        mod.thickness = 0.006
        mod.use_replace = True
        for obj in (solid, wire):
            overlay_only(obj)  # don't let ghosts cast shadows on the beam
        return solid, wire

    def _build_vrml_elements(self, meshes, to_world):
        seen_names = set()
        for k, m in enumerate(meshes):
            verts = [to_world(v) for v in m["verts"]]
            name = m.get("name") or f"element_{k}"
            if m["faces"]:
                mesh = bpy.data.meshes.new(f"elem_{k}_{name}")
                mesh.from_pydata(verts, [], [tuple(f) for f in m["faces"]])
                mesh.validate()
                self._ghost_pair(f"elem_{k}_{name}", mesh,
                                 m.get("color") or (0.3, 0.6, 0.9))
            for poly in m["lines"]:
                cu = bpy.data.curves.new(f"elem_{k}_{name}_lines", "CURVE")
                cu.dimensions = "3D"
                cu.bevel_depth = 0.006
                cu.materials.append(self.mats["elem_wire"])
                sp = cu.splines.new("POLY")
                sp.points.add(len(poly) - 1)
                for pt, vi in zip(sp.points, poly):
                    pt.co = (*verts[vi], 1.0)
                overlay_only(link(bpy.data.objects.new(
                    f"elem_{k}_{name}_lines", cu)))
            if m.get("name") and m["name"] not in seen_names:
                seen_names.add(m["name"])

    def _build_csv_elements(self, items, to_world, s_trans, s_long, cam):
        for k, el in enumerate(items):
            center = Vector(to_world((el["x"], el["y"], el["z"])))
            length_b = max(el["length"] * s_long, 0.02)
            bm = bmesh.new()
            if el["shape"] == "box":
                res = bmesh.ops.create_cube(bm, size=1.0)
                bmesh.ops.scale(bm, verts=res["verts"],
                                vec=(2 * el["rout"] * s_trans, length_b,
                                     2 * el["height"] * s_trans))
            else:
                radii = [el["rout"]]
                if el["shape"] == "tube" and el["rin"] > 0:
                    radii.append(el["rin"])
                for r in radii:
                    res = bmesh.ops.create_cone(
                        bm, cap_ends=False, segments=40,
                        radius1=r * s_trans, radius2=r * s_trans, depth=length_b)
                    bmesh.ops.rotate(
                        bm, verts=res["verts"], cent=(0, 0, 0),
                        matrix=Matrix.Rotation(math.radians(90.0), 3, "X"))
            mesh = bpy.data.meshes.new(f"elem_{k}_{el['name']}")
            bm.to_mesh(mesh)
            bm.free()
            solid, wire = self._ghost_pair(f"elem_{k}_{el['name']}", mesh,
                                           (0.20, 0.45, 0.85))
            for obj in (solid, wire):
                obj.location = center
            top = el["rout"] * s_trans if el["shape"] != "box" \
                else el["height"] * s_trans
            make_text(f"elem_{k}_label", el["name"], 0.28,
                      center + Vector((0, 0, top + 0.45)),
                      self.mats["elem_label"], target=cam)

    # -- phase-space stations ------------------------------------------------

    def station_center(self, index):
        col = index % STATION_COLS
        row = index // STATION_COLS
        return Vector((STATION_ORIGIN[0] + col * STATION_SPACING,
                       STATION_ORIGIN[1] - row * STATION_SPACING, 0.0))

    def build_station(self, vid, index, channels):
        """channels: one channel dict per beam (see compute_channels)."""
        vdef = VIEW_DEFS[vid]
        axes = vdef["axes"]
        center = self.station_center(index)
        # Axes are normalized over all beams together, so both blobs share the
        # same scale and are directly comparable.
        norms = [axis_norm([ch[c] for ch in channels]) for c, _ in axes]

        beam_coords = []
        for b, ch in enumerate(channels):
            n = len(ch["x"][0])
            coords = []
            for s in range(self.S):
                row = []
                for i in range(n):
                    vals = [(ch[c][s][i] - norms[a][0]) * norms[a][2]
                            for a, (c, _) in enumerate(axes)]
                    row.append((vals[0], vals[1], vals[2]))
                coords.append(row)
            beam_coords.append(coords)
            objs = make_view_objects(
                f"{vid}_b{b}", coords, self.sample_frames, center,
                radius=0.0085, particle_mat=self.mats[f"particle{b}"],
                hull_mat=self.mats[f"hull{b}"], with_hull=self.with_hull,
                spans=self.beams[b]["span"])
            for obj in objs:
                move_to_collection(obj, self.beam_collection(b))

        axmat, txmat = self.mats["axis"], self.mats["text"]
        tgt = make_empty(f"{vid}_target", center)
        # The camera hangs off a pivot that slowly orbits the station -- through
        # the fades and the end hold too -- so the 3D shape of the evolving
        # hull reads clearly in the animation.
        pivot = make_empty(f"{vid}_campivot", center)
        pivot.rotation_euler = (0.0, 0.0, math.radians(-11.0))
        pivot.keyframe_insert("rotation_euler", frame=1)
        pivot.rotation_euler = (0.0, 0.0, math.radians(16.0))
        pivot.keyframe_insert("rotation_euler", frame=self.total_frames)
        set_interpolation(pivot.animation_data, "LINEAR")
        cam = make_camera(f"Cam_{vid}", (7.1, -9.6, 5.1), target=tgt, lens=40)
        cam.parent = pivot
        self.cameras[vid] = cam

        def range_label(a):
            ch, label = axes[a]
            cen, half, _ = norms[a]
            return f"{label}\n[{fmt(cen - half)}, {fmt(cen + half)}]"

        ext = STATION_HALF + 0.45  # axis arrows slightly past the data cube
        corner = center + Vector((-ext, -ext, -ext))
        dirs = [Vector((1, 0, 0)), Vector((0, 1, 0)), Vector((0, 0, 1))]
        for a, d in enumerate(dirs):
            make_arrow(f"{vid}_ax{a}", corner, d, 2 * ext, 0.02, axmat)
        make_text(f"{vid}_lbl0", range_label(0), 0.21,
                  corner + Vector((2 * ext + 0.6, 0, 0.8)), txmat, target=cam)
        # The depth-axis label is lifted above the hull and pushed left so
        # the camera can always see it.
        make_text(f"{vid}_lbl1", range_label(1), 0.21,
                  corner + Vector((-2.45, 2 * ext + 0.6, 2 * ext - 0.95)),
                  txmat, target=cam)
        make_text(f"{vid}_lbl2", range_label(2), 0.21,
                  corner + Vector((-0.8, 0, ext)), txmat, target=cam)

        # Title + z-readout + 2D sub-projection panels live on the camera as a
        # heads-up display, fixed in frame while the camera orbits.
        self.build_station_hud(vid, cam, axes, beam_coords)
        make_cylinder(f"{vid}_pedestal", center + Vector((0, 0, FLOOR_Z + 0.2)),
                      radius=3.1, depth=0.4, material=self.mats["pedestal"])
        return center

    def build_stations(self):
        station_views = [v for v in VIEW_ORDER if v in self.views]
        if not station_views:
            return
        channels = [compute_channels(beam, self.centroids)
                    for beam in self.beams]
        centers = [self.build_station(vid, i, channels)
                   for i, vid in enumerate(station_views)]

        lo = Vector((min(c.x for c in centers), min(c.y for c in centers), 0))
        hi = Vector((max(c.x for c in centers), max(c.y for c in centers), 0))
        mid = (lo + hi) / 2.0
        extent = max(hi.x - lo.x, hi.y - lo.y, STATION_SPACING)

        tgt = make_empty("StationsTarget", mid + Vector((0, 0, 0.3)))
        dist = extent * 0.95 + 8.0
        cam = make_camera("Cam_overview",
                          mid + Vector((0, -dist, dist * 0.62)),
                          target=tgt, lens=38)
        self.cameras["overview"] = cam
        self.hud_collection("overview")
        self.make_overlay("overview", lens=38.0)
        self.add_hud_text("overview", "overview_title",
                          "Phase space — all projections", 0.105, 0.0, 0.66, 3.0,
                          bold=True)
        self.add_z_readout("overview", "overview", 0.075, 0.0, 0.555, 3.0)
        self.add_legend("overview", "overview", -1.3, 0.66, 3.0, 0.075)

        # Warm three-point studio rig (Kelvin temperatures, Blender >= 4.5).
        self.lights.append(make_area_light(
            "StationsKey", mid + Vector((4, -6, 14)), tgt,
            size=extent + 14, power=22000, temperature=3100.0))
        self.lights.append(make_area_light(
            "StationsFill", mid + Vector((-extent, -10, 7)), tgt,
            size=14, power=6000, temperature=3700.0))
        self.lights.append(make_area_light(
            "StationsRim", mid + Vector((0, extent * 0.8 + 8, 5)), tgt,
            size=18, power=8000, temperature=4200.0))

    def setup_fades(self):
        """The whole scene wakes up from black and returns to black: lamps,
        every emissive material (particles, wireframes, HUD and the
        composited text overlays) and the world background all ramp together."""
        if self.fade_in_f <= 0 and self.fade_out_f <= 0:
            return
        f_on = 1 + self.fade_in_f
        f_off = self.total_frames - self.fade_out_f

        def ramp(holder, prop, full):
            if self.fade_in_f > 0:
                setattr(holder, prop, 0.0)
                holder.keyframe_insert(prop, frame=1)
                setattr(holder, prop, full)
                holder.keyframe_insert(prop, frame=f_on)
            if self.fade_out_f > 0:
                setattr(holder, prop, full)
                holder.keyframe_insert(prop, frame=f_off)
                setattr(holder, prop, 0.0)
                holder.keyframe_insert(prop, frame=self.total_frames)

        for obj in self.lights:
            ramp(obj.data, "energy", obj.data.energy)

        for mat in bpy.data.materials:
            if not mat.node_tree:
                continue
            bsdf = next((n for n in mat.node_tree.nodes
                         if n.type == "BSDF_PRINCIPLED"), None)
            if bsdf is None:
                continue
            sock = bsdf.inputs.get("Emission Strength")
            if sock is None or sock.default_value <= 0.0:
                continue
            ramp(sock, "default_value", sock.default_value)

        world = bpy.context.scene.world
        if world and world.node_tree:
            bg = world.node_tree.nodes.get("Background")
            if bg:
                ramp(bg.inputs[1], "default_value", bg.inputs[1].default_value)

    def build_floor(self):
        make_plane("Floor", (0, -14, FLOOR_Z), 320, self.mats["floor"])

    # -- output --------------------------------------------------------------

    def save(self):
        out = os.path.abspath(self.args.output)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        bpy.ops.wm.save_as_mainfile(filepath=out, compress=True)
        print(f"[scene_builder] saved {out}")

    def set_active_hud(self, label):
        """Show only the HUD panels and text overlay of `label`'s camera."""
        for vid, col in self.hud_cols.items():
            hidden = vid != label
            col.hide_render = hidden
            col.hide_viewport = hidden
        ov = self.overlays.get(label)
        if ov is not None and self.comp_ov_rl is not None:
            self.comp_ov_rl.scene = ov["scene"]
            self.comp_ov_rl.layer = ov["scene"].view_layers[0].name

    def render(self):
        scene = bpy.context.scene
        wanted = (list(self.cameras) if self.args.cameras == "all"
                  else [c for c in self.args.cameras.split(",") if c])
        rdir = os.path.abspath(self.args.render_dir)
        os.makedirs(rdir, exist_ok=True)
        present = [w for w in wanted if w in self.cameras]
        print(f"[scene_builder] render plan: {len(present)} camera(s) x "
              f"{scene.frame_end - scene.frame_start + 1} frames", flush=True)

        # Per-frame marker for the CLI progress bar: Blender's own console
        # output is block-buffered through a pipe and its format varies
        # between versions, so we emit our own flushed line per written frame.
        def _frame_written(*_args):
            print(f"[scene_builder] frame-done {scene.frame_current}",
                  flush=True)

        bpy.app.handlers.render_write.append(_frame_written)
        try:
            self._render_cameras(scene, wanted, rdir)
        finally:
            try:
                bpy.app.handlers.render_write.remove(_frame_written)
            except ValueError:
                pass
        print(f"[scene_builder] renders written to {rdir}", flush=True)

    def _set_ffmpeg_output(self, scene, filepath):
        ims = scene.render.image_settings
        if hasattr(ims, "media_type"):  # Blender >= 5.0
            ims.media_type = "VIDEO"
        ims.file_format = "FFMPEG"
        scene.render.ffmpeg.format = "MPEG4"
        scene.render.ffmpeg.codec = "H264"
        scene.render.ffmpeg.constant_rate_factor = "HIGH"
        scene.render.ffmpeg.audio_codec = "NONE"
        scene.render.filepath = filepath

    def _encode_pngs_to_mp4(self, frames_dir, prefix, out_filepath):
        """Encode an already-rendered PNG sequence to an MP4 with Blender's
        sequencer -- no Cycles, so 'both' stays a single render pass. Used
        because the compositor File Output node's API is too version-specific."""
        frames = sorted(f for f in os.listdir(frames_dir)
                        if f.startswith(prefix) and f.endswith(".png"))
        if not frames:
            print("[scene_builder] no PNG frames to encode; skipping MP4")
            return
        scene = bpy.context.scene
        enc = bpy.data.scenes.new("BunchEncode")
        enc.frame_start, enc.frame_end = 1, len(frames)
        enc.render.fps = self.args.fps
        enc.render.resolution_x = scene.render.resolution_x
        enc.render.resolution_y = scene.render.resolution_y
        enc.render.use_sequencer = True
        enc.render.use_compositing = False
        self._set_ffmpeg_output(enc, out_filepath)
        se = enc.sequence_editor_create()
        coll = se.strips if hasattr(se, "strips") else se.sequences
        strip = coll.new_image(name="seq", channel=1, frame_start=1,
                               filepath=os.path.join(frames_dir, frames[0]))
        for f in frames[1:]:
            strip.elements.append(f)
        # Suspend the per-frame progress markers so the cheap encode pass
        # doesn't inflate the CLI progress bar's frame count.
        saved = list(bpy.app.handlers.render_write)
        bpy.app.handlers.render_write.clear()
        try:
            with bpy.context.temp_override(scene=enc):
                bpy.ops.render.render(animation=True)
        finally:
            bpy.app.handlers.render_write.extend(saved)
            bpy.data.scenes.remove(enc)

    def _render_cameras(self, scene, wanted, rdir):
        fmt = self.args.format
        ims = scene.render.image_settings
        for label in wanted:
            cam = self.cameras.get(label)
            if cam is None:
                print(f"[scene_builder] no camera '{label}' in this scene; skipped")
                continue
            scene.camera = cam
            self.set_active_hud(label)
            if fmt == "mp4":
                self._set_ffmpeg_output(scene, os.path.join(rdir, f"{label}_"))
            else:  # png or both -> render the PNG sequence first
                if hasattr(ims, "media_type"):
                    ims.media_type = "IMAGE"
                ims.file_format = "PNG"
                scene.render.filepath = os.path.join(rdir, label, "frame_")
            print(f"[scene_builder] rendering camera '{label}' "
                  f"({scene.frame_start}-{scene.frame_end}) -> "
                  f"{scene.render.filepath}...", flush=True)
            bpy.ops.render.render(animation=True)
            if fmt == "both":
                mp4 = os.path.join(rdir, f"{label}_"
                                   f"{scene.frame_start:04d}-{scene.frame_end:04d}.mp4")
                print(f"[scene_builder] encoding '{label}' frames -> {mp4}",
                      flush=True)
                self._encode_pngs_to_mp4(os.path.join(rdir, label), "frame_", mp4)

    def run(self):
        load_fonts()
        self.setup_scene()
        self.setup_materials()
        if "beam" in self.views:
            self.build_beam_view()
        self.build_stations()
        self.build_floor()
        self.setup_fades()
        scene = bpy.context.scene
        default = "beam" if "beam" in self.cameras else next(iter(self.cameras), None)
        scene.camera = self.cameras.get(default)
        self.set_active_hud(default)
        self.save()
        if self.args.render:
            self.render()
            self.set_active_hud(default)


def main():
    args = parse_args()
    if bpy.app.version < (4, 0, 0):
        print(f"[scene_builder] warning: Blender {bpy.app.version_string} detected; "
              "BunchRenderer targets Blender >= 4.0 and may fail on older versions.")
    Builder(args).run()


if __name__ == "__main__":
    main()
