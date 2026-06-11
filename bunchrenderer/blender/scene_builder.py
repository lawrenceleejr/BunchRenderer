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

Requires Blender >= 4.0; >= 4.5 recommended (true Kelvin light temperatures).
"""

import argparse
import json
import math
import os
import sys

import bpy
import bmesh
from mathutils import Vector

FLOOR_Z = -2.6           # world height of the studio floor
STATION_HALF = 1.5       # data is normalized into a cube of this half-extent
STATION_SPACING = 14.0
STATION_COLS = 3
STATION_ORIGIN = (-STATION_SPACING, -30.0)  # x of first column, y of first row
BEAM_LENGTH = 28.0       # corridor length in blender units
BEAM_HALF_TRANSVERSE = 1.2
BEAM_HEIGHT = 1.6

PALETTE = {
    "hull": (0.55, 0.60, 0.68),
    "beam_particle": (1.0, 0.62, 0.18),
    "phase_particle": (0.20, 0.85, 1.0),
    "axis": (0.82, 0.84, 0.88),
    "text": (0.92, 0.94, 1.0),
}


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
    p.add_argument("--render", action="store_true")
    p.add_argument("--render-dir", default="renders")
    p.add_argument("--format", choices=("mp4", "png"), default="mp4")
    return p.parse_args(argv)


# --------------------------------------------------------------------------
# Small helpers (data-API only; no operators that need UI context)

def link(obj):
    bpy.context.scene.collection.objects.link(obj)
    return obj


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
    for fc in key.animation_data.action.fcurves:
        for kp in fc.keyframe_points:
            kp.interpolation = "LINEAR"
    return obj


def make_view_objects(name, coords, sample_frames, center, radius,
                      particle_mat, hull_mat, with_hull):
    """Particle cloud + optional convex-hull envelope sharing one animated mesh."""
    cloud = make_cloud(f"{name}_particles", coords, sample_frames)
    cloud.location = center
    mod = cloud.modifiers.new("Points", "NODES")
    mod.node_group = points_node_group(f"{name}_points", radius, particle_mat)
    objs = [cloud]
    if with_hull:
        hull = bpy.data.objects.new(f"{name}_hull", cloud.data)
        link(hull)
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
              align_x="CENTER", extrude=0.012, parent=None):
    cu = bpy.data.curves.new(name, "FONT")
    cu.body = body
    cu.size = size
    cu.align_x = align_x
    cu.align_y = "CENTER"
    cu.extrude = extrude
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
    mesh.materials.append(material)
    obj = bpy.data.objects.new(name, mesh)
    obj.location = location
    return link(obj)


def make_plane(name, location, size, material):
    mesh = bpy.data.meshes.new(name)
    h = size / 2.0
    mesh.from_pydata([(-h, -h, 0), (h, -h, 0), (h, h, 0), (-h, h, 0)],
                     [], [(0, 1, 2, 3)])
    mesh.materials.append(material)
    obj = bpy.data.objects.new(name, mesh)
    obj.location = location
    return link(obj)


def fmt(v):
    return f"{v:+.3g}" if abs(v) >= 1e-12 else "0"


# --------------------------------------------------------------------------
# Phase-space channels

def compute_channels(data):
    """Per-sample, per-particle phase-space coordinates from pos/mom arrays."""
    pos, mom = data["pos"], data["mom"]
    S, N = len(pos), len(pos[0])
    ch = {k: [[0.0] * N for _ in range(S)] for k in
          ("x", "y", "xp", "yp", "dz", "pz")}
    for s in range(S):
        zc = sum(p[2] for p in pos[s]) / N
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


def axis_norm(channel_data):
    """Symmetric center/scale so the full evolution fits in the station cube."""
    lo = min(min(row) for row in channel_data)
    hi = max(max(row) for row in channel_data)
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
        self.S = self.data["meta"]["n_samples"]
        self.N = self.data["meta"]["n_particles"]
        f0, f1 = 1, args.frames
        self.sample_frames = [f0 + (f1 - f0) * s / (self.S - 1)
                              for s in range(self.S)]
        self.cameras = {}
        self.mats = {}
        self.with_hull = not args.no_hull
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
        try:
            scene.cycles.device = "CPU"
        except Exception:
            pass
        scene.render.use_motion_blur = True
        scene.render.motion_blur_shutter = 0.55
        scene.render.fps = self.args.fps
        scene.frame_start = 1
        scene.frame_end = self.args.frames
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
            scene.use_nodes = True
            tree = scene.node_tree
            tree.nodes.clear()
            rl = tree.nodes.new("CompositorNodeRLayers")
            glare = tree.nodes.new("CompositorNodeGlare")
            glare.glare_type = "FOG_GLOW"
            if glare.inputs.get("Strength") is not None:  # Blender >= 4.4 sockets
                glare.inputs["Threshold"].default_value = 1.0
                glare.inputs["Strength"].default_value = 0.18
                glare.inputs["Size"].default_value = 0.6
            else:  # Blender <= 4.3 node properties
                glare.quality = "MEDIUM"
                glare.threshold = 1.0
                glare.size = 8
                glare.mix = -0.7
            comp = tree.nodes.new("CompositorNodeComposite")
            tree.links.new(rl.outputs["Image"], glare.inputs["Image"])
            tree.links.new(glare.outputs["Image"], comp.inputs["Image"])
        except Exception as exc:
            print(f"[scene_builder] compositor glare skipped: {exc}")

    def setup_materials(self):
        self.mats["hull"] = make_material(
            "BR_Hull", PALETTE["hull"], metallic=1.0, roughness=0.32,
            alpha=0.42, coat=0.25)
        self.mats["beam_particle"] = make_material(
            "BR_BeamParticle", PALETTE["beam_particle"], metallic=0.6,
            roughness=0.3, emission=PALETTE["beam_particle"], emission_strength=2.2)
        self.mats["phase_particle"] = make_material(
            "BR_PhaseParticle", PALETTE["phase_particle"], metallic=0.6,
            roughness=0.3, emission=PALETTE["phase_particle"], emission_strength=3.0)
        self.mats["axis"] = make_material(
            "BR_Axis", PALETTE["axis"], metallic=0.9, roughness=0.35)
        self.mats["text"] = make_material(
            "BR_Text", PALETTE["text"], metallic=0.0, roughness=0.5,
            emission=PALETTE["text"], emission_strength=1.2)
        self.mats["floor"] = make_material(
            "BR_Floor", (0.030, 0.032, 0.038), metallic=0.85, roughness=0.28)
        self.mats["pedestal"] = make_material(
            "BR_Pedestal", (0.06, 0.065, 0.075), metallic=0.7, roughness=0.4)
        self.mats["guide"] = make_material(
            "BR_Guide", (0.4, 0.7, 1.0), emission=(0.4, 0.7, 1.0),
            emission_strength=0.6)

    # -- beam (real space) view ---------------------------------------------

    def build_beam_view(self):
        pos = self.data["pos"]
        S, N = self.S, self.N
        xs = [p[0] for row in pos for p in row]
        ys = [p[1] for row in pos for p in row]
        zs = [p[2] for row in pos for p in row]
        x0 = sum(xs) / len(xs)
        y0 = sum(ys) / len(ys)
        zmin, zmax = min(zs), max(zs)
        s_long = BEAM_LENGTH / max(zmax - zmin, 1e-9)
        half_dev = max(max(abs(v - x0) for v in xs),
                       max(abs(v - y0) for v in ys), 1e-9)
        s_trans = BEAM_HALF_TRANSVERSE / half_dev
        exag = s_trans / s_long

        def to_world(p):
            return ((p[0] - x0) * s_trans,
                    (p[2] - zmin) * s_long,
                    BEAM_HEIGHT + (p[1] - y0) * s_trans)

        coords = [[to_world(p) for p in row] for row in pos]
        make_view_objects("beam", coords, self.sample_frames, (0, 0, 0),
                          radius=0.045, particle_mat=self.mats["beam_particle"],
                          hull_mat=self.mats["hull"], with_hull=self.with_hull)

        centroids = [Vector((sum(c[0] for c in row) / N,
                             sum(c[1] for c in row) / N,
                             sum(c[2] for c in row) / N))
                     for row in coords]

        # Camera rig: an empty rides the bunch centroid; the camera is parented
        # to it at an offset and tracks it, so motion blur comes for free.
        target = make_empty("BeamTarget", centroids[0])
        for s, frame in enumerate(self.sample_frames):
            target.location = centroids[s]
            target.keyframe_insert("location", frame=frame)
        for fc in target.animation_data.action.fcurves:
            for kp in fc.keyframe_points:
                kp.interpolation = "LINEAR"

        cam = make_camera("Cam_beam", (0, 0, 0), target=target, lens=46)
        cam.parent = target
        cam.location = (-4.6, -7.5, 2.7)
        cam.data.dof.use_dof = True
        cam.data.dof.focus_object = target
        cam.data.dof.aperture_fstop = 4.0
        self.cameras["beam"] = cam

        # Faint guide curve showing the full centroid trajectory.
        curve = bpy.data.curves.new("BeamGuide", "CURVE")
        curve.dimensions = "3D"
        curve.bevel_depth = 0.009
        curve.materials.append(self.mats["guide"])
        spline = curve.splines.new("POLY")
        spline.points.add(len(centroids) - 1)
        for pt, c in zip(spline.points, centroids):
            pt.co = (c.x, c.y, c.z, 1.0)
        link(bpy.data.objects.new("BeamGuide", curve))

        # Labeled axis tripod and titles that ride along with the bunch
        # (parented to the camera target) so they stay in frame.
        axmat, txmat = self.mats["axis"], self.mats["text"]
        tripod = Vector((-3.0, 1.2, -0.9))
        make_arrow("beam_ax_x", tripod, (1, 0, 0), 1.5, 0.022, axmat, parent=target)
        make_arrow("beam_ax_y", tripod, (0, 0, 1), 1.5, 0.022, axmat, parent=target)
        make_arrow("beam_ax_zdir", tripod, (0, 1, 0), 1.5, 0.022, axmat, parent=target)
        make_text("beam_lbl_x", "x", 0.3, tripod + Vector((1.85, 0, 0)),
                  txmat, target=cam, parent=target)
        make_text("beam_lbl_y", "y", 0.3, tripod + Vector((0, 0, 1.85)),
                  txmat, target=cam, parent=target)
        make_text("beam_lbl_zdir", "z", 0.3, tripod + Vector((0, 1.85, 0)),
                  txmat, target=cam, parent=target)
        make_text("beam_title", "Real space — beam frame", 0.34,
                  Vector((2.6, 0.5, 1.5)), txmat, target=cam, parent=target)
        make_text("beam_caption", f"transverse scale ×{exag:.0f}", 0.2,
                  Vector((2.6, 0.5, 1.05)), txmat, target=cam, parent=target)

        # Static ruler along the corridor with real-world z positions.
        origin = Vector(centroids[0])
        make_arrow("beam_ax_z", origin, (0, 1, 0), BEAM_LENGTH + 1.5, 0.016, axmat)
        for k in range(5):
            f = k / 4.0
            zval = (zmin + f * (zmax - zmin)) / 1000.0  # meters
            tick = origin + Vector((0, f * BEAM_LENGTH, 0))
            make_arrow(f"beam_tick_{k}", tick + Vector((0, 0, -0.3)),
                       (0, 0, 1), 0.6, 0.012, axmat)
            make_text(f"beam_ticklbl_{k}", f"z = {zval:.3g} m", 0.22,
                      tick + Vector((0.0, 0, -0.66)), txmat, target=cam)

        # Corridor lighting: warm key lights along the flight path with a
        # slightly cooler (but still warm) rim from the side.
        for k, yfrac in enumerate((0.15, 0.5, 0.85)):
            loc = (2.5, BEAM_LENGTH * yfrac, BEAM_HEIGHT + 8.0)
            make_area_light(f"BeamKey_{k}", loc, target, size=9.0, power=3000,
                            temperature=3100.0)
        make_area_light("BeamRim", (-6.0, BEAM_LENGTH * 0.7, BEAM_HEIGHT + 1.5),
                        target, size=12.0, power=1500, temperature=3900.0)

    # -- phase-space stations ------------------------------------------------

    def station_center(self, index):
        col = index % STATION_COLS
        row = index // STATION_COLS
        return Vector((STATION_ORIGIN[0] + col * STATION_SPACING,
                       STATION_ORIGIN[1] - row * STATION_SPACING, 0.0))

    def build_station(self, vid, index, channels):
        vdef = VIEW_DEFS[vid]
        axes = vdef["axes"]
        center = self.station_center(index)
        norms = [axis_norm(channels[ch]) for ch, _ in axes]

        coords = []
        for s in range(self.S):
            row = []
            for i in range(self.N):
                vals = [(channels[ch][s][i] - norms[a][0]) * norms[a][2]
                        for a, (ch, _) in enumerate(axes)]
                row.append((vals[0], vals[1], vals[2]))
            coords.append(row)

        make_view_objects(vid, coords, self.sample_frames, center,
                          radius=0.034, particle_mat=self.mats["phase_particle"],
                          hull_mat=self.mats["hull"], with_hull=self.with_hull)

        axmat, txmat = self.mats["axis"], self.mats["text"]
        tgt = make_empty(f"{vid}_target", center)
        # The camera hangs off a pivot that slowly orbits the station, so the
        # 3D shape of the evolving hull reads clearly in the animation.
        pivot = make_empty(f"{vid}_campivot", center)
        pivot.rotation_euler = (0.0, 0.0, math.radians(-11.0))
        pivot.keyframe_insert("rotation_euler", frame=1)
        pivot.rotation_euler = (0.0, 0.0, math.radians(14.0))
        pivot.keyframe_insert("rotation_euler", frame=self.args.frames)
        for fc in pivot.animation_data.action.fcurves:
            for kp in fc.keyframe_points:
                kp.interpolation = "LINEAR"
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
                  corner + Vector((-1.95, 2 * ext + 0.6, 2 * ext - 0.95)),
                  txmat, target=cam)
        make_text(f"{vid}_lbl2", range_label(2), 0.21,
                  corner + Vector((-0.8, 0, ext)), txmat, target=cam)

        make_text(f"{vid}_title", vdef["title"], 0.28,
                  center + Vector((0, 0, ext + 0.95)), txmat, target=cam)
        make_cylinder(f"{vid}_pedestal", center + Vector((0, 0, FLOOR_Z + 0.2)),
                      radius=3.1, depth=0.4, material=self.mats["pedestal"])
        return center

    def build_stations(self):
        station_views = [v for v in VIEW_ORDER if v in self.views]
        if not station_views:
            return
        channels = compute_channels(self.data)
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

        # Warm three-point studio rig (Kelvin temperatures, Blender >= 4.5).
        make_area_light("StationsKey", mid + Vector((4, -6, 14)), tgt,
                        size=extent + 14, power=22000, temperature=3100.0)
        make_area_light("StationsFill", mid + Vector((-extent, -10, 7)), tgt,
                        size=14, power=6000, temperature=3700.0)
        make_area_light("StationsRim", mid + Vector((0, extent * 0.8 + 8, 5)), tgt,
                        size=18, power=8000, temperature=4200.0)

    def build_floor(self):
        make_plane("Floor", (0, -14, FLOOR_Z), 320, self.mats["floor"])

    # -- output --------------------------------------------------------------

    def save(self):
        out = os.path.abspath(self.args.output)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        bpy.ops.wm.save_as_mainfile(filepath=out, compress=True)
        print(f"[scene_builder] saved {out}")

    def render(self):
        scene = bpy.context.scene
        wanted = (list(self.cameras) if self.args.cameras == "all"
                  else [c for c in self.args.cameras.split(",") if c])
        rdir = os.path.abspath(self.args.render_dir)
        os.makedirs(rdir, exist_ok=True)
        for label in wanted:
            cam = self.cameras.get(label)
            if cam is None:
                print(f"[scene_builder] no camera '{label}' in this scene; skipped")
                continue
            scene.camera = cam
            if self.args.format == "mp4":
                scene.render.image_settings.file_format = "FFMPEG"
                scene.render.ffmpeg.format = "MPEG4"
                scene.render.ffmpeg.codec = "H264"
                scene.render.ffmpeg.constant_rate_factor = "HIGH"
                scene.render.ffmpeg.audio_codec = "NONE"
                scene.render.filepath = os.path.join(rdir, f"{label}_")
            else:
                scene.render.image_settings.file_format = "PNG"
                scene.render.filepath = os.path.join(rdir, label, "frame_")
            print(f"[scene_builder] rendering camera '{label}' "
                  f"({scene.frame_start}-{scene.frame_end})...")
            bpy.ops.render.render(animation=True)
        print(f"[scene_builder] renders written to {rdir}")

    def run(self):
        self.setup_scene()
        self.setup_materials()
        if "beam" in self.views:
            self.build_beam_view()
        self.build_stations()
        self.build_floor()
        scene = bpy.context.scene
        scene.camera = self.cameras.get("beam") or next(iter(self.cameras.values()), None)
        self.save()
        if self.args.render:
            self.render()


def main():
    args = parse_args()
    if bpy.app.version < (4, 0, 0):
        print(f"[scene_builder] warning: Blender {bpy.app.version_string} detected; "
              "BunchRenderer targets Blender >= 4.0 and may fail on older versions.")
    Builder(args).run()


if __name__ == "__main__":
    main()
