# BunchRenderer

Turn particle-beam track data into animated, studio-lit **Blender** scenes.

Feed it a [g4beamline](http://g4beamline.muonsinc.com) track file (or a folder
of per-track CSVs) and it builds a `.blend` file containing:

* **Real-space beam flight** — the bunch traveling through 3D space, chased by
  a camera with motion blur and depth of field, with a ruler showing real
  `z` positions and an axis tripod riding alongside the bunch.
* **3D projections of the 4D transverse phase space** — all four 3-subsets of
  `(x, x′, y, y′)`, plus the longitudinal projections `(Δz, Pz, x)` and
  `(Δz, Pz, y)`, each on its own lit pedestal.
* **Everything animated** — every view evolves along the beamline over the
  timeline, and each station camera slowly orbits its pedestal so the 3D
  shape of the distribution reads clearly in motion.
* **Convex-hull envelopes** — every view wraps the particle ensemble in a
  soft-metallic, semi-transparent convex hull, recomputed *every frame* with
  geometry nodes, so you can watch the beam envelope rotate, shear and breathe
  in phase space as the bunch propagates.
* **Labeled axes** on every view (names, units, and the real data ranges), a
  title per view, and an `overview` dashboard camera framing all stations.

The whole scene uses Cycles with a warm three-point studio lighting rig (real
Kelvin color temperatures, 3100–4200 K), a reflective floor, AgX color
management, and a subtle glow pass.

Blender runs **inside Docker by default** (no local install needed); a local
Blender can be used instead with `--local`.

## Install

```bash
pip install .
```

The CLI itself has no Python dependencies. You need either Docker (default
mode) or a local Blender (`--local` mode) — version ≥ 4.5 recommended (real
Kelvin light temperatures); ≥ 4.0 works with an approximated warm tint.

## Quick start

```bash
# Generate the bundled example: a Gaussian muon bunch in a focusing channel
python examples/make_gaussian_beam.py

# Build a .blend (default mode: Blender inside Docker)
bunchrender examples/gaussian_beam.txt -o beam.blend

# ... or use your local Blender install
bunchrender examples/gaussian_beam.txt -o beam.blend --local
bunchrender examples/gaussian_beam.txt -o beam.blend --blender /opt/blender-4.2/blender

# Headless mode: also render the animation (one movie per camera)
bunchrender examples/gaussian_beam.txt -o beam.blend --render
bunchrender examples/gaussian_beam.txt --render --cameras beam,overview --format mp4
```

Open the `.blend` in Blender, pick a camera (`Cam_beam`, `Cam_overview`,
`Cam_xxp`, ...) with `Ctrl+Numpad 0`, and press `Space` to watch the beam
evolve. By default **only the `.blend` is produced**; `--render` switches on
the fully headless render.

On first use the default mode builds a small Docker image
(`bunchrenderer/blender:4.5.10`, Ubuntu + official Blender LTS build). Use
`--docker-image IMAGE` to substitute any image that provides a `blender`
command on its `PATH`.

## Input formats

**g4beamline BLTrackFile** (ASCII): comment lines start with `#`; the column
header is read from the comments, falling back to the standard order
`x y z Px Py Pz t PDGid EventID TrackID ParentID Weight`
(mm, MeV/c, ns). Rows are grouped into tracks by `(EventID, TrackID)` and
sorted by time — so a file written by several `virtualdetector`/`zntuple`
planes, or by a track-stepping file, animates directly.

**Directory of CSVs**: one file per particle track, in time order. Columns are
matched by header name (`x, y, z, Px, Py, Pz, t`, case-insensitive, units in
brackets ignored; `x'`/`xp` divergences in mrad are also accepted). Headerless
files are interpreted positionally as `x y z [px py pz] [t]`. Missing momenta
are inferred from the flight direction; missing times from path length.

**Single-plane beam files** (one row per particle, e.g. a beam sampled at one
virtual detector): each particle is ballistically drifted `--drift-length` mm
along its momentum so there is still motion to animate.

All tracks are resampled onto a common uniform time grid (`--time-samples`),
so every view shows the true *simultaneous* state of the bunch.

## Views and cameras

| id | camera | contents |
|----|--------|----------|
| `beam` | `Cam_beam` (follow-cam, motion blur, DoF) | bunch in real space + hull + z ruler |
| `xxpy` | `Cam_xxpy` (orbiting) | 3D projection (x, x′, y) of 4D phase space |
| `xxpyp` | `Cam_xxpyp` (orbiting) | 3D projection (x, x′, y′) of 4D phase space |
| `xyyp` | `Cam_xyyp` (orbiting) | 3D projection (x, y, y′) of 4D phase space |
| `xpyyp` | `Cam_xpyyp` (orbiting) | 3D projection (x′, y, y′) of 4D phase space |
| `zpzx` | `Cam_zpzx` (orbiting) | longitudinal phase space (Δz, Pz, x) |
| `zpzy` | `Cam_zpzy` (orbiting) | longitudinal phase space (Δz, Pz, y) |
| — | `Cam_overview` | dashboard framing all phase-space stations |

All stations animate in lock-step with the beam flight: scrubbing the timeline
(or rendering with `--render`) shows the distributions evolving along the
beamline in every projection simultaneously.

Select what gets built with `--views xxpy,beam,...` and what gets rendered in
headless mode with `--cameras beam,overview,...` (default: all). Axis labels
show the real units and the `[min, max]` range each station is normalized to;
the beam view prints its transverse exaggeration factor (beams are long and
thin — transverse coordinates are magnified to stay visible).

## Options

```text
bunchrender INPUT [-o out.blend]
  --local                 use local Blender instead of Docker
  --blender PATH          Blender executable (implies --local)
  --docker-image IMAGE    alternative Docker image with `blender` on PATH
  --render                headless mode: render the animation after building
  --render-dir DIR        render output location (default <output>_renders)
  --cameras LIST          cameras to render (default all)
  --format mp4|png        movie per camera, or PNG frame folders
  --views LIST            which views to build (default all)
  --no-hull               skip the convex-hull envelopes
  --frames N --fps N      animation length (default 240 @ 24 fps)
  --samples N             Cycles samples (default 64, denoised)
  --resolution WxH        default 1920x1080
  --max-particles N       cap particles kept in the scene (default 300)
  --time-samples N        resampling grid (default 100)
  --drift-length MM       drift used for single-point-per-particle inputs
```

## Example data

`examples/gaussian_beam.txt` is a 150-muon Gaussian bunch tracked through a
uniform focusing channel, regenerable (with different parameters, or as a CSV
folder) via:

```bash
python examples/make_gaussian_beam.py --particles 300 --stations 80 --csv-dir tracks_csv
```

Because the channel focuses, the x–x′ and y–y′ ellipses rotate at different
rates as the bunch flies — watch the hulls in the phase-space stations.

## How it works

The CLI parses and resamples the tracks to JSON, stages it with a scene-builder
script in a temp directory, and launches `blender -b --factory-startup
--python scene_builder.py` (in Docker, the staging directory is the only bind
mount). Inside Blender, each view is a mesh with one vertex per particle,
animated through the time samples with absolute shape keys; geometry-node
modifiers turn it into renderable points and a per-frame convex hull. Hull
deformation motion blur is disabled (its topology changes every frame);
camera and particle motion blur stay on.
