# BunchRenderer

Turn particle-beam track data into animated, studio-lit **Blender** scenes.

Feed it a [g4beamline](http://g4beamline.muonsinc.com) track file (or a folder
of per-track CSVs) and it builds a `.blend` file — or headlessly renders
movies — containing:

* **Real-space beam flight** — the bunch traveling through 3D space, chased by
  a camera with motion blur and depth of field, a ruler showing real `z`
  positions, an axis tripod riding alongside the bunch, and (optionally) the
  **beamline geometry superimposed Tron-style**: low-opacity ghost solids with
  brighter emissive wireframes, read directly from g4beamline's VRML export.
* **3D projections of the 4D transverse phase space** — all four 3-subsets of
  `(x, x′, y, y′)`, plus the longitudinal projections `(Δz, Pz, x)` and
  `(Δz, Pz, y)`, each on its own lit pedestal with a slowly orbiting camera.
* **A heads-up display on every station camera** — the view title and a live
  `z = … m` readout locked to the frame (they don't rotate with the scene),
  plus the three flat 2D sub-projections of that 3D view as small animated
  panels along the right edge.
* **Convex-hull envelopes** — every view (and every HUD panel) wraps the
  ensemble in a soft-metallic convex hull recomputed *every frame*, so you
  watch the beam envelope rotate, shear and breathe as the bunch propagates.
* **A polished timeline** — warm Kelvin-temperature studio lights ramp on
  (~0.75 s), the beam evolves along the beamline, the final state holds while
  the cameras keep orbiting, and the lights fade to black (~1.5 s) leaving
  the emissive particles and HUD glowing.

Everything renders with Cycles, AgX color management, a reflective floor and
a subtle glow pass.

## Running on macOS (no Docker)

1. Install [Blender 4.5 LTS](https://www.blender.org/download/lts/4-5/)
   (or `brew install --cask blender`). No Python packages are needed — the
   CLI is pure standard library.
2. From a clone of this repo, run it directly (no `pip install` required):

```bash
git clone https://github.com/lawrenceleejr/BunchRenderer
cd BunchRenderer

# headlessly render every view of your track file to MP4s:
./bin/bunchrender /path/to/tracks.txt --render \
    --blender /Applications/Blender.app/Contents/MacOS/Blender

# with the beamline geometry superimposed on the real-space view:
./bin/bunchrender /path/to/tracks.txt --render --elements g4_00.wrl \
    --blender /Applications/Blender.app/Contents/MacOS/Blender
```

`--blender PATH` implies local (non-Docker) mode; `python3 -m bunchrenderer …`
is equivalent to `./bin/bunchrender …`. Movies land in `<output>_renders/`,
one MP4 per camera, alongside the `.blend`.

### If `pip install .` fails reaching an internal package index

On machines whose pip is pinned to an internal index (e.g. CERN's acc-py),
`pip install .` dies offsite because build isolation tries to download
`setuptools` from that index. Point this one install at PyPI instead:

```bash
pip install -i https://pypi.org/simple .
```

Alternatives: `pip install --no-build-isolation .` (uses your existing
setuptools, no network at all), or fix it permanently with
`pip config set global.index-url https://pypi.org/simple`
(`pip config debug` shows which config file pins the index). Or skip
installing entirely — `./bin/bunchrender` runs straight from the clone.

## Quick start (any platform)

```bash
# Generate the bundled example: a Gaussian muon bunch in a focusing channel
python3 examples/make_gaussian_beam.py

# Default mode: build only the .blend, using Blender inside Docker
bunchrender examples/gaussian_beam.txt -o beam.blend

# Local Blender instead of Docker
bunchrender examples/gaussian_beam.txt --local                  # 'blender' on PATH
bunchrender examples/gaussian_beam.txt --blender /opt/blender/blender

# Headless mode: also render the animation (one movie per camera)
bunchrender examples/gaussian_beam.txt --render
bunchrender examples/gaussian_beam.txt --render --cameras beam,xxpy --format png

# Superimpose the example beamline geometry
bunchrender examples/gaussian_beam.txt --render \
    --elements examples/beamline_solenoids.wrl
```

By default **only the `.blend` is produced**; `--render` switches on the fully
headless render. On first use, Docker mode builds a small image
(`bunchrenderer/blender:4.5.10`, Ubuntu + the official Blender LTS build);
`--docker-image IMAGE` substitutes any image with `blender` on its `PATH`.

## Input formats

**g4beamline BLTrackFile** (ASCII): comment lines start with `#`; the column
header is read from the comments, falling back to the standard order
`x y z Px Py Pz t PDGid EventID TrackID ParentID Weight` (mm, MeV/c, ns).
Rows are grouped into tracks by `(EventID, TrackID)` and sorted by time, so a
file written by several `virtualdetector`/`zntuple` planes animates directly.

**Directory of CSVs**: one file per particle track, in time order. Columns are
matched by header name (`x, y, z, Px, Py, Pz, t`, case-insensitive, units in
brackets ignored; `x'`/`xp` divergences in mrad also accepted). Headerless
files are read positionally as `x y z [px py pz] [t]`. Missing momenta are
inferred from the flight direction; missing times from path length.

**Single-plane beam files** (one row per particle): each particle is
ballistically drifted `--drift-length` mm along its momentum so there is still
motion to animate.

All tracks are resampled onto a common uniform time grid (`--time-samples`),
so every view shows the true *simultaneous* state of the bunch.

## Beamline geometry (`--elements`)

Two formats:

**VRML from g4beamline (recommended).** Add `viewer=VRML1FILE` to your g4bl
run (e.g. `g4bl my_deck.g4bl viewer=VRML1FILE`); Geant4 writes `g4_00.wrl`
with every visible volume as a world-frame polyhedron in mm — the same
coordinates as the track file. Pass it straight in:

```bash
bunchrender tracks.txt --render --elements g4_00.wrl
```

Solids become low-opacity ghosts tinted with their g4bl colors, overlaid with
brighter emissive wireframes. Shapes wider than `--elements-max-radius`
(default 1000 mm) are dropped so enclosure/world volumes don't swallow the
scene.

**Hand-written CSV** for quick sketches (`examples/beamline_elements.csv`):

```csv
name,shape,z,length,rin,rout,height
Solenoid 1,tube,450,500,40,60,
Collimator,box,100,80,0,70,70
```

Columns in mm: `shape` is `tube` (inner+outer wall), `cylinder` or `box`;
`z`/`length` along the beamline; `rout` is the outer radius (box: half-width);
`height` the box half-height; optional `x,y` offset columns. CSV elements get
floating name labels in the scene.

Note the real-space view exaggerates transverse coordinates (beams are long
and thin); element radii are scaled identically, so apertures stay correct
*relative to the beam size*, and the printed `transverse scale ×N` applies to
both.

## Views and cameras

| id | camera | contents |
|----|--------|----------|
| `beam` | `Cam_beam` (follow-cam, motion blur, DoF) | bunch in real space + hull + ruler + elements |
| `xxpy` | `Cam_xxpy` (orbiting) | 3D projection (x, x′, y) of 4D phase space |
| `xxpyp` | `Cam_xxpyp` (orbiting) | 3D projection (x, x′, y′) of 4D phase space |
| `xyyp` | `Cam_xyyp` (orbiting) | 3D projection (x, y, y′) of 4D phase space |
| `xpyyp` | `Cam_xpyyp` (orbiting) | 3D projection (x′, y, y′) of 4D phase space |
| `zpzx` | `Cam_zpzx` (orbiting) | longitudinal phase space (Δz, Pz, x) |
| `zpzy` | `Cam_zpzy` (orbiting) | longitudinal phase space (Δz, Pz, y) |
| — | `Cam_overview` | dashboard framing all phase-space stations |

All stations animate in lock-step with the beam flight; the HUD `z` readout
shows where along the beamline the displayed distribution lives. In-scene
axis labels show units and the `[min, max]` ranges each station is normalized
to. Titles, readouts and the 2D sub-projection panels are parented to each
camera, so they stay fixed in the frame while the camera orbits.

Each camera's overlay lives in a collection named `HUD_<camera>`; headless
rendering enables the right one automatically. If you render a camera by hand
inside Blender, enable its `HUD_*` collection (and disable the others) in the
outliner.

## Timeline

```text
|-- fade-in --|------------ evolution ------------|-- hold --|-- fade-out --|
   lights on     bunch travels, phase space         final       lights off,
   (0.75 s)      evolves, cameras orbit             state       glow remains
                 (--frames / --fps)                 (0.5 s)     (1.5 s)
```

Tune with `--fade-in`, `--hold`, `--fade-out` (seconds; 0 disables a phase).
Cameras orbit through the entire timeline, including the hold and fades.

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
  --elements FILE         beamline geometry: g4bl VRML (.wrl) or CSV
  --elements-max-radius MM  drop shapes wider than this (default 1000)
  --no-hull               skip the convex-hull envelopes
  --no-hud                skip the 2D sub-projection HUD panels
  --frames N --fps N      evolution length (default 240 @ 24 fps)
  --fade-in/--hold/--fade-out SEC   timeline polish (0.75 / 0.5 / 1.5)
  --samples N             Cycles samples (default 64, denoised)
  --resolution WxH        default 1920x1080
  --max-particles N       cap particles kept in the scene (default 300)
  --time-samples N        resampling grid (default 100)
  --drift-length MM       drift used for single-point-per-particle inputs
```

## Example data

`examples/gaussian_beam.txt` is a 150-muon Gaussian bunch tracked through a
uniform focusing channel — the x–x′ and y–y′ ellipses rotate at different
rates as the bunch flies, so every projection visibly evolves. Regenerate
(or emit a per-track CSV folder) with:

```bash
python3 examples/make_gaussian_beam.py --particles 300 --stations 80 --csv-dir tracks_csv
```

`examples/beamline_solenoids.wrl` and `examples/beamline_elements.csv` are
matching beamline layouts in both supported geometry formats:

```bash
bunchrender examples/gaussian_beam.txt --render --elements examples/beamline_solenoids.wrl
```

## How it works

The CLI parses and resamples the tracks (plus any geometry) to JSON, stages it
with a scene-builder script in a temp directory, and launches
`blender -b --factory-startup --python scene_builder.py` — in Docker, the
staging directory is the only bind mount. Inside Blender, each view is a mesh
with one vertex per particle, animated through the time samples with absolute
shape keys; geometry-node modifiers turn it into renderable points and a
per-frame convex hull. Light fades are keyframed Kelvin-temperature area
lights (Blender ≥ 4.5; older versions get a blackbody-RGB approximation).
Hull deformation motion blur is disabled (its topology changes every frame);
camera and particle motion blur stay on. The animated `z` readout is a
sequence of camera-locked texts with stepped visibility keyframes, since text
bodies are not animatable in Blender.
