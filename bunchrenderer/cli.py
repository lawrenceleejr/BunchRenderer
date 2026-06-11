"""bunchrender command-line interface.

Builds a Blender scene from particle-track data, running Blender inside a
Docker container by default or via a local Blender install with ``--local``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import __version__, elements as elementslib, tracks as tracklib

BLENDER_VERSION = "4.5.10"
DEFAULT_IMAGE = f"bunchrenderer/blender:{BLENDER_VERSION}"

VIEW_IDS = ["beam", "xxpy", "xxpyp", "xyyp", "xpyyp", "zpzx", "zpzy"]
CAMERA_IDS = VIEW_IDS + ["overview"]


def _pkg_path(*parts):
    return Path(__file__).resolve().parent.joinpath(*parts)


def build_parser():
    p = argparse.ArgumentParser(
        prog="bunchrender",
        description="Build an animated Blender scene from g4beamline track data: "
                    "the beam flying through real space plus labeled phase-space "
                    "projections, each with an animated convex-hull envelope.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("input",
                   help="g4beamline track file (BLTrackFile ASCII), or a directory "
                        "of CSV files with one particle track per file")
    p.add_argument("-o", "--output", default=None,
                   help="output .blend path (default: <input name>.blend)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    ex = p.add_argument_group("execution")
    ex.add_argument("--local", action="store_true",
                    help="use a local Blender install instead of Docker")
    ex.add_argument("--blender", default=None, metavar="PATH",
                    help="path to the Blender executable (implies --local; "
                         "default: 'blender' on PATH)")
    ex.add_argument("--docker-image", default=None, metavar="IMAGE",
                    help=f"Docker image providing a 'blender' command (default: "
                         f"{DEFAULT_IMAGE}, built automatically on first use)")

    rd = p.add_argument_group("headless rendering")
    rd.add_argument("--render", action="store_true",
                    help="headless mode: render the animation after building the "
                         "scene (default: only produce the .blend file)")
    rd.add_argument("--render-dir", default=None, metavar="DIR",
                    help="where to put rendered output (default: <output stem>_renders)")
    rd.add_argument("--cameras", default="all",
                    help="comma-separated cameras to render: "
                         + ",".join(CAMERA_IDS) + " or 'all'")
    rd.add_argument("--format", choices=("mp4", "png"), default="mp4",
                    help="rendered output format")

    sc = p.add_argument_group("scene")
    sc.add_argument("--views", default="all",
                    help="comma-separated views to build: "
                         + ",".join(VIEW_IDS) + " or 'all'")
    sc.add_argument("--elements", default=None, metavar="FILE",
                    help="beamline geometry superimposed on the real-space view: "
                         "a VRML 1.0 .wrl exported by g4beamline (viewer=VRML1FILE) "
                         "or a simple CSV (see README)")
    sc.add_argument("--elements-max-radius", type=float, default=1000.0, metavar="MM",
                    help="skip geometry shapes larger than this transverse "
                         "half-extent (filters out world/enclosure volumes)")
    sc.add_argument("--no-hull", action="store_true",
                    help="skip the convex-hull envelope surfaces")
    sc.add_argument("--no-hud", action="store_true",
                    help="skip the 2D sub-projection HUD panels on the "
                         "phase-space station cameras")
    sc.add_argument("--frames", type=int, default=240,
                    help="length of the beam-evolution part of the animation, "
                         "in frames (fades and hold are added on top)")
    sc.add_argument("--fps", type=int, default=24, help="frames per second")
    sc.add_argument("--fade-in", type=float, default=0.75, metavar="SEC",
                    help="lights-on ramp before the beam evolution starts "
                         "(0 disables)")
    sc.add_argument("--hold", type=float, default=0.5, metavar="SEC",
                    help="hold on the final beam state (cameras keep orbiting) "
                         "before the fade-out")
    sc.add_argument("--fade-out", type=float, default=1.5, metavar="SEC",
                    help="lights-off ramp at the end of the animation "
                         "(0 disables)")
    sc.add_argument("--samples", type=int, default=64, help="Cycles render samples")
    sc.add_argument("--resolution", default="1920x1080", metavar="WxH",
                    help="render resolution")

    da = p.add_argument_group("input data")
    da.add_argument("--max-particles", type=int, default=300,
                    help="cap on the number of particles kept in the scene")
    da.add_argument("--time-samples", type=int, default=100,
                    help="number of time samples the tracks are resampled onto")
    da.add_argument("--drift-length", type=float, default=2000.0, metavar="MM",
                    help="ballistic drift length used when the input contains only "
                         "a single point per particle (e.g. a beam at one plane)")
    return p


def _validate_csv_list(value, allowed, flag):
    if value.strip().lower() == "all":
        return "all"
    items = [v.strip() for v in value.split(",") if v.strip()]
    bad = [v for v in items if v not in allowed]
    if bad:
        raise SystemExit(f"error: unknown {flag} value(s): {', '.join(bad)} "
                         f"(choose from {', '.join(allowed)})")
    return ",".join(items)


def _builder_args(args, data_path, blend_path, render_dir):
    out = ["--data", str(data_path), "--output", str(blend_path),
           "--frames", str(args.frames), "--fps", str(args.fps),
           "--fade-in", str(args.fade_in), "--hold", str(args.hold),
           "--fade-out", str(args.fade_out),
           "--samples", str(args.samples), "--resolution", args.resolution,
           "--views", args.views, "--cameras", args.cameras,
           "--format", args.format, "--render-dir", str(render_dir)]
    if args.no_hull:
        out.append("--no-hull")
    if args.no_hud:
        out.append("--no-hud")
    if args.render:
        out.append("--render")
    return out


def _mesh_transverse_half_extent(mesh):
    xs = [v[0] for v in mesh["verts"]]
    ys = [v[1] for v in mesh["verts"]]
    return max(max(xs) - min(xs), max(ys) - min(ys), 0.0) / 2.0


def _load_elements(args):
    """Load and filter the beamline geometry, if requested."""
    if not args.elements:
        return None
    bundle = elementslib.load_elements(args.elements)
    rmax = args.elements_max_radius
    if bundle["type"] == "vrml":
        kept = [m for m in bundle["meshes"]
                if _mesh_transverse_half_extent(m) <= rmax]
        dropped = len(bundle["meshes"]) - len(kept)
        if dropped:
            print(f"[bunchrender] elements: dropped {dropped} shape(s) wider "
                  f"than {rmax:.0f} mm (see --elements-max-radius)")
        if not kept:
            raise elementslib.ElementsError(
                "all geometry shapes were filtered out; raise --elements-max-radius")
        bundle["meshes"] = kept
        print(f"[bunchrender] elements: {len(kept)} shape(s) from VRML")
    else:
        kept = [e for e in bundle["items"] if e["rout"] <= rmax]
        dropped = len(bundle["items"]) - len(kept)
        if dropped:
            print(f"[bunchrender] elements: dropped {dropped} element(s) wider "
                  f"than {rmax:.0f} mm (see --elements-max-radius)")
        bundle["items"] = kept
        print(f"[bunchrender] elements: {len(kept)} element(s) from CSV")
    return bundle


def _image_exists(image):
    r = subprocess.run(["docker", "image", "inspect", image],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return r.returncode == 0


def _ensure_default_image():
    if _image_exists(DEFAULT_IMAGE):
        return
    context = _pkg_path("docker")
    print(f"[bunchrender] Docker image {DEFAULT_IMAGE} not found; building it "
          f"(one-time, downloads Blender {BLENDER_VERSION})...")
    subprocess.run(["docker", "build",
                    "--build-arg", f"BLENDER_VERSION={BLENDER_VERSION}",
                    "-t", DEFAULT_IMAGE, str(context)], check=True)


def _run_docker(args, staging, builder_args):
    if shutil.which("docker") is None:
        raise SystemExit("error: docker not found on PATH. Install Docker, or use "
                         "--local to run a local Blender install.")
    image = args.docker_image
    if image is None:
        image = DEFAULT_IMAGE
        _ensure_default_image()
    cmd = ["docker", "run", "--rm"]
    if hasattr(os, "getuid"):
        cmd += ["-u", f"{os.getuid()}:{os.getgid()}"]
    cmd += ["-e", "HOME=/tmp",
            "-v", f"{staging}:/work", "-w", "/work", image,
            "blender", "-b", "--factory-startup",
            "--python", "/work/scene_builder.py", "--"] + builder_args
    return subprocess.call(cmd)


def _run_local(args, staging, builder_args):
    blender = args.blender or "blender"
    if shutil.which(blender) is None and not Path(blender).exists():
        raise SystemExit(f"error: Blender executable not found: {blender!r}. "
                         "Use --blender PATH to point at your install.")
    cmd = [blender, "-b", "--factory-startup",
           "--python", str(staging / "scene_builder.py"), "--"] + builder_args
    return subprocess.call(cmd)


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.views = _validate_csv_list(args.views, VIEW_IDS, "--views")
    args.cameras = _validate_csv_list(args.cameras, CAMERA_IDS, "--cameras")
    args.frames = max(2, args.frames)
    args.time_samples = max(2, args.time_samples)
    args.max_particles = max(4, args.max_particles)

    in_path = Path(args.input)
    out_path = Path(args.output) if args.output else Path.cwd() / (in_path.stem or "bunch")
    if out_path.suffix != ".blend":
        out_path = out_path.with_suffix(".blend")
    out_path = out_path.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    render_dir = (Path(args.render_dir).resolve() if args.render_dir
                  else out_path.with_name(out_path.stem + "_renders"))

    try:
        tracks = tracklib.load_tracks(in_path, drift_length=args.drift_length)
        bundle = tracklib.resample(tracks, n_samples=args.time_samples,
                                   max_particles=args.max_particles)
        elements = _load_elements(args)
        if elements is not None:
            bundle["elements"] = elements
    except (tracklib.TrackError, elementslib.ElementsError) as exc:
        raise SystemExit(f"error: {exc}")

    meta = bundle["meta"]
    print(f"[bunchrender] {meta['n_particles']} particles, "
          f"{meta['n_samples']} time samples, "
          f"t = {meta['t_start_ns']:.3f} .. {meta['t_end_ns']:.3f} ns")

    # Stage everything Blender needs in one directory so a single Docker bind
    # mount covers data in and artifacts out.
    staging = Path(tempfile.mkdtemp(prefix=".bunchrenderer-", dir=out_path.parent))
    try:
        (staging / "data.json").write_text(json.dumps(bundle))
        shutil.copy(_pkg_path("blender", "scene_builder.py"),
                    staging / "scene_builder.py")
        (staging / "renders").mkdir()

        use_local = args.local or args.blender is not None
        if use_local:
            builder_args = _builder_args(args, staging / "data.json",
                                         staging / "scene.blend",
                                         staging / "renders")
            rc = _run_local(args, staging, builder_args)
        else:
            builder_args = _builder_args(args, "/work/data.json",
                                         "/work/scene.blend", "/work/renders")
            rc = _run_docker(args, staging, builder_args)
        if rc != 0:
            raise SystemExit(f"error: Blender exited with status {rc}")

        blend_tmp = staging / "scene.blend"
        if not blend_tmp.exists():
            raise SystemExit("error: Blender did not produce a .blend file")
        shutil.move(str(blend_tmp), str(out_path))
        print(f"[bunchrender] wrote {out_path}")

        if args.render:
            produced = [p for p in sorted((staging / "renders").rglob("*")) if p.is_file()]
            if produced:
                render_dir.mkdir(parents=True, exist_ok=True)
                shutil.copytree(staging / "renders", render_dir, dirs_exist_ok=True)
                print(f"[bunchrender] wrote {len(produced)} render file(s) to {render_dir}")
            else:
                print("[bunchrender] warning: no render output was produced")
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
