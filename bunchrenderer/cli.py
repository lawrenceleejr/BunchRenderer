"""bunchrender command-line interface.

Builds a Blender scene from particle-track data, running Blender inside a
Docker container by default or via a local Blender install with ``--local``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import deque
from pathlib import Path

from . import __version__, elements as elementslib, tracks as tracklib

BLENDER_VERSION = "5.0.1"
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
    p.add_argument("inputs", nargs="+", metavar="INPUT",
                   help="one or more g4beamline track files (BLTrackFile ASCII) "
                        "or directories of per-track CSVs; each input becomes "
                        "its own color-coded beam in every view")
    p.add_argument("-o", "--output", default=None,
                   help="output .blend path (default: <first input name>.blend)")
    p.add_argument("--labels", default=None, metavar="A,B",
                   help="comma-separated display names for the beams "
                        "(default: input file names)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    ex = p.add_argument_group("execution")
    ex.add_argument("-v", "--verbose", action="store_true",
                    help="stream Blender's raw output instead of the progress "
                         "bar (everything is always saved to the .log file)")
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
    rd.add_argument("--gpu", action="store_true",
                    help="render on the GPU (Metal/CUDA/OptiX/HIP/oneAPI; "
                         "best with --local — Docker needs a GPU runtime)")

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
    if args.gpu:
        out.append("--gpu")
    if args.render:
        out.append("--render")
    return out


def _load_beams(args, in_paths, labels):
    """Load every input and resample all beams onto one shared time grid."""
    track_sets = [tracklib.load_tracks(p, drift_length=args.drift_length)
                  for p in in_paths]
    t0 = min(tracklib.time_range(ts)[0] for ts in track_sets)
    t1 = max(tracklib.time_range(ts)[1] for ts in track_sets)
    per_beam_cap = max(10, args.max_particles // len(track_sets))

    beams = []
    times = None
    for label, tracks in zip(labels, track_sets):
        b = tracklib.resample(tracks, n_samples=args.time_samples,
                              max_particles=per_beam_cap, t_range=(t0, t1))
        times = b["times"]
        beams.append({"label": label,
                      "n_particles": b["meta"]["n_particles"],
                      "pdgid": b["meta"]["pdgid"],
                      "pos": b["pos"], "mom": b["mom"]})
    return {
        "meta": {
            "n_beams": len(beams),
            "labels": labels,
            "n_samples": args.time_samples,
            "t_start_ns": round(t0, 4),
            "t_end_ns": round(t1, 4),
            "units": {"pos": "mm", "mom": "MeV/c", "t": "ns"},
        },
        "times": times,
        "beams": beams,
    }


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


def _fmt_dur(seconds):
    if seconds is None:
        return "--:--"
    seconds = int(seconds)
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class _ProgressReporter:
    """Parses Blender's output into a progress bar with ETA, teeing the raw
    log to a file. Works with both Blender 4.x ('Fra:', 'Append frame') and
    5.x ('cat | Saved: ...') console formats."""

    RE_PLAN = re.compile(r"\[scene_builder\] render plan: (\d+) camera\(s\) x (\d+) frames")
    RE_CAM = re.compile(r"\[scene_builder\] rendering camera '([^']+)'")
    RE_DONE = re.compile(r"Saved: '|Append frame \d")
    RE_FRA = re.compile(r"\bFra:(\d+)")
    BAR_W = 26

    def __init__(self, log_path, verbose):
        self.verbose = verbose
        self.log = open(log_path, "w", errors="replace")
        self.tty = sys.stdout.isatty() and not verbose
        self.t0 = time.monotonic()
        self.t_render = None
        self.n_cams = self.frames_per_cam = self.total = self.done = 0
        self.cam_idx = 0
        self.cam_label = ""
        self.recent = deque(maxlen=48)  # timestamps of recent frame completions
        self.last_draw = 0.0
        self.tail = deque(maxlen=30)
        self.bar_active = False
        self.build_announced = False

    def _emit(self, text):
        if self.bar_active:
            sys.stdout.write("\r\x1b[K")
            self.bar_active = False
        sys.stdout.write(text.rstrip() + "\n")
        sys.stdout.flush()

    def feed(self, line):
        self.log.write(line)
        s = line.strip()
        if s:
            self.tail.append(s)
        if self.verbose:
            sys.stdout.write(line)
            sys.stdout.flush()

        m = self.RE_PLAN.search(s)
        if m:
            self.n_cams, self.frames_per_cam = int(m.group(1)), int(m.group(2))
            self.total = self.n_cams * self.frames_per_cam
            self.t_render = time.monotonic()
            if not self.verbose:
                self._emit(f"[bunchrender] rendering {self.n_cams} camera(s) × "
                           f"{self.frames_per_cam} frames = {self.total} frames")
            return
        m = self.RE_CAM.search(s)
        if m:
            self.cam_idx += 1
            self.cam_label = m.group(1)
            if not self.verbose:
                self._emit(s)
            self._progress()
            return
        if self.RE_DONE.search(s):
            self.done += 1
            self.recent.append(time.monotonic())
            self._progress()
            return
        if self.verbose:
            return
        if "[scene_builder] saved" in s and not self.build_announced:
            self.build_announced = True
            self._emit(f"[bunchrender] scene built in "
                       f"{_fmt_dur(time.monotonic() - self.t0)}")
            self._emit(s)
            return
        if (s.startswith("[scene_builder]") or "Traceback" in s
                or s.startswith("Error:")):
            self._emit(s)

    def _eta(self):
        now = time.monotonic()
        if len(self.recent) >= 2:
            rate = (len(self.recent) - 1) / max(self.recent[-1] - self.recent[0], 1e-6)
        elif self.done and self.t_render:
            rate = self.done / max(now - self.t_render, 1e-6)
        else:
            return None, None
        remaining = (self.total - self.done) / max(rate, 1e-9)
        return remaining, (now - self.t0) + remaining

    def _progress(self):
        if self.verbose or not self.total:
            return
        now = time.monotonic()
        if self.tty:
            if now - self.last_draw < 0.25 and self.done < self.total:
                return
            self.last_draw = now
            frac = self.done / self.total
            filled = int(frac * self.BAR_W)
            bar = "█" * filled + "·" * (self.BAR_W - filled)
            remaining, total_est = self._eta()
            sys.stdout.write(
                f"\r\x1b[K[{bar}] {frac * 100:5.1f}%  "
                f"cam {self.cam_idx}/{self.n_cams} {self.cam_label:<8.8s} "
                f"{self.done}/{self.total}f  "
                f"elapsed {_fmt_dur(now - self.t0)}  "
                f"ETA {_fmt_dur(remaining)}  total ~{_fmt_dur(total_est)}")
            sys.stdout.flush()
            self.bar_active = True
        else:
            # No TTY: a plain line every ~5% / 60 s, suitable for log files.
            step = max(1, self.total // 20)
            if self.done % step and now - self.last_draw < 60:
                return
            self.last_draw = now
            remaining, total_est = self._eta()
            print(f"[bunchrender] {self.done / self.total * 100:5.1f}% "
                  f"({self.done}/{self.total} frames, cam {self.cam_idx}/"
                  f"{self.n_cams} {self.cam_label}) elapsed "
                  f"{_fmt_dur(now - self.t0)} ETA {_fmt_dur(remaining)} "
                  f"total ~{_fmt_dur(total_est)}", flush=True)

    def finish(self, rc):
        if self.bar_active:
            sys.stdout.write("\n")
            self.bar_active = False
        self.log.close()
        now = time.monotonic()
        if rc != 0 and not self.verbose:
            print("[bunchrender] Blender failed; last output lines:")
            for s in list(self.tail)[-15:]:
                print(f"    {s}")
        elif self.done and self.t_render:
            dt = now - self.t_render
            print(f"[bunchrender] rendered {self.done} frames in {_fmt_dur(dt)} "
                  f"({dt / self.done:.1f} s/frame)")


def _stream(cmd, reporter):
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            errors="replace", bufsize=1)
    try:
        for line in proc.stdout:
            reporter.feed(line)
    finally:
        proc.stdout.close()
    return proc.wait()


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


def _docker_cmd(args, staging, out_path, render_dir, builder_args):
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
            "-v", f"{staging}:/work",
            "-v", f"{out_path.parent}:/out"]
    if args.render:
        cmd += ["-v", f"{render_dir}:/render"]
    cmd += ["-w", "/work", image,
            "blender", "-b", "--factory-startup",
            "--python", "/work/scene_builder.py", "--"] + builder_args
    return cmd


def _run_local(args, staging, builder_args):
    blender = args.blender or "blender"
    if shutil.which(blender) is None and not Path(blender).exists():
        raise SystemExit(f"error: Blender executable not found: {blender!r}. "
                         "Use --blender PATH to point at your install.")
    return [blender, "-b", "--factory-startup",
            "--python", str(staging / "scene_builder.py"), "--"] + builder_args


def main(argv=None):
    args = build_parser().parse_args(argv)
    # Announce which copy of the code is running: a non-editable
    # `pip install .` snapshots the package, so after `git pull` a stale
    # install is the usual cause of "I'm still seeing the old bug".
    print(f"[bunchrender] {__version__} running from {_pkg_path()}")
    args.views = _validate_csv_list(args.views, VIEW_IDS, "--views")
    args.cameras = _validate_csv_list(args.cameras, CAMERA_IDS, "--cameras")
    args.frames = max(2, args.frames)
    args.time_samples = max(2, args.time_samples)
    args.max_particles = max(4, args.max_particles)

    in_paths = [Path(p) for p in args.inputs]
    out_path = (Path(args.output) if args.output
                else Path.cwd() / (in_paths[0].stem or "bunch"))
    if out_path.suffix != ".blend":
        out_path = out_path.with_suffix(".blend")
    out_path = out_path.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    render_dir = (Path(args.render_dir).resolve() if args.render_dir
                  else out_path.with_name(out_path.stem + "_renders"))

    labels = ([s.strip() for s in args.labels.split(",")] if args.labels
              else [p.stem or f"beam {i}" for i, p in enumerate(in_paths)])
    if len(labels) != len(in_paths):
        raise SystemExit("error: --labels must name each input exactly once")

    try:
        bundle = _load_beams(args, in_paths, labels)
        elements = _load_elements(args)
        if elements is not None:
            bundle["elements"] = elements
    except (tracklib.TrackError, elementslib.ElementsError) as exc:
        raise SystemExit(f"error: {exc}")

    meta = bundle["meta"]
    for beam in bundle["beams"]:
        print(f"[bunchrender] beam '{beam['label']}': "
              f"{beam['n_particles']} particles")
    print(f"[bunchrender] {meta['n_samples']} time samples, "
          f"t = {meta['t_start_ns']:.3f} .. {meta['t_end_ns']:.3f} ns")

    # Staging holds only the inputs Blender needs (track data + builder
    # script). The .blend and all renders are written directly to their final
    # destinations, so long renders can be watched as they progress and a
    # crash loses nothing that was already rendered.
    staging = Path(tempfile.mkdtemp(prefix=".bunchrenderer-", dir=out_path.parent))
    try:
        (staging / "data.json").write_text(json.dumps(bundle))
        shutil.copy(_pkg_path("blender", "scene_builder.py"),
                    staging / "scene_builder.py")
        if args.render:
            render_dir.mkdir(parents=True, exist_ok=True)
            print(f"[bunchrender] renders will appear in {render_dir} as they "
                  "progress (PNG frames per frame; each MP4 finalizes when its "
                  "camera finishes)")

        use_local = args.local or args.blender is not None
        if use_local:
            builder_args = _builder_args(args, staging / "data.json",
                                         out_path, render_dir)
            cmd = _run_local(args, staging, builder_args)
        else:
            builder_args = _builder_args(args, "/work/data.json",
                                         f"/out/{out_path.name}", "/render")
            cmd = _docker_cmd(args, staging, out_path, render_dir, builder_args)

        log_path = out_path.with_suffix(".log")
        reporter = _ProgressReporter(log_path, args.verbose)
        rc = _stream(cmd, reporter)
        reporter.finish(rc)
        if rc != 0:
            raise SystemExit(f"error: Blender exited with status {rc} "
                             f"(full log: {log_path})")

        if not out_path.exists():
            raise SystemExit("error: Blender did not produce a .blend file")
        print(f"[bunchrender] wrote {out_path}")

        if args.render:
            produced = [p for p in sorted(render_dir.rglob("*")) if p.is_file()]
            if produced:
                print(f"[bunchrender] {len(produced)} render file(s) in {render_dir}")
            else:
                print("[bunchrender] warning: no render output was produced")
        print(f"[bunchrender] total time {_fmt_dur(time.monotonic() - reporter.t0)} "
              f"(Blender log: {log_path})")
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
