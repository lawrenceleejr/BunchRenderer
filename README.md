# BunchRenderer

Turn particle-beam track data into animated, studio-lit **Blender** scenes.

Feed it one or more [g4beamline](http://g4beamline.muonsinc.com) track files
(or folders of per-track CSVs) and it builds a `.blend` file — or headlessly
renders movies — containing:

* **Real-space beam flight** — the bunch traveling through 3D space, chased by
  a camera (motion blur + depth of field) that dollies straight down the
  beamline keeping pace with the leading particle, plus a second **axial
  camera** (`Cam_beam_axial`) looking down the beam axis — the view that makes
  transverse (Larmor) rotation obvious. A ruler shows real `z`, an axis tripod
  is pinned in-frame, and (optionally) the **beamline geometry superimposed**:
  faint additive ghost solids with thin emissive wireframes from g4beamline's
  VRML export.
* **Particle trails** (`--trails N`) — each particle drags a comet tail that
  fades over its previous `N` time samples, so its swept path is drawn out.
  This makes rotation legible even when the input is sparsely sampled in `z`
  (the discrete positions connect into visible helices); a very large `N`
  traces the entire history.
* **3D projections of the 4D transverse phase space** — all four 3-subsets of
  `(x, x′, y, y′)`, plus the longitudinal projections `(Δz, Pz, x)` and
  `(Δz, Pz, y)`, each on its own lit pedestal with a slowly orbiting camera.
* **A heads-up display on every camera** — the view title, a live `z = … m`
  readout and the beam legend are **flat 2D text composited on top of each
  frame** (rendered in a separate orthographic overlay scene and alpha-combined
  in the compositor — no depth, no perspective, no motion blur), plus the
  three 2D sub-projections of each 3D view as small animated panels along the
  right edge.
* **Convex-hull envelopes** — every view (and every HUD panel) wraps the
  ensemble in a soft-metallic convex hull recomputed *every frame*, so you
  watch the beam envelope rotate, shear and breathe as the bunch propagates.
  Particles that have stopped propagating (lost/absorbed — their track ends
  or their position freezes) drop out of the hull so a few stragglers don't
  bloat the envelope, while still showing as points where they stopped.
* **Multi-beam comparison** — pass several track files and each becomes its
  own color-coded beam (amber, cyan, green, magenta) rendered into the *same*
  views: every 3D projection shows all the blobs on shared axes, every camera
  HUD carries a color legend, and each beam's objects live in their own
  Blender collection. Beams are resampled onto one shared clock.
* **Off-axis trajectories** — the real-space view separates bunch size from
  centroid excursion, so beams traveling centimeters off the reference axis
  are shown as real excursions around the straight reference ruler rather than
  flattened onto it. The chase camera dollies *straight* down the beamline
  (it never corkscrews), so the bunch's transverse motion reads as the bunch
  moving within the frame. Nothing is imposed on the data — whatever path the
  tracks contain is what you see.
* **A polished timeline** — the scene wakes up from black (~0.75 s: lamps,
  every emissive material and the text overlays ramp together), the beam
  evolves along the beamline, the final state holds while the cameras keep
  orbiting, and everything fades back to black (~1.5 s).

Everything renders with Cycles, AgX color management, a reflective floor and
a subtle glow pass. All text is set in [Lora](https://fonts.google.com/specimen/Lora)
(OFL, bundled and packed into the `.blend`) — Bold for titles, Regular for
labels and readouts.

## Running on macOS (no Docker)

1. Install [Blender 5](https://www.blender.org/download/) (tested on 5.0.1;
   `brew install --cask blender` is fine). Blender 4.2–4.5 LTS also work,
   with 4.2–4.4 approximating the Kelvin lights. No Python packages are
   needed — the CLI is pure standard library.
2. From a clone of this repo, run it directly (no `pip install` required):

```bash
git clone https://github.com/lawrenceleejr/BunchRenderer
cd BunchRenderer

# headlessly render every view of your track file to MP4s, on the GPU:
./bin/bunchrender /path/to/tracks.txt --render --gpu \
    --blender /Applications/Blender.app/Contents/MacOS/Blender

# quick draft to check it before a full render (--llq is faster/rougher):
./bin/bunchrender /path/to/tracks.txt --render --lq \
    --blender /Applications/Blender.app/Contents/MacOS/Blender

# with the beamline geometry superimposed on the real-space view:
./bin/bunchrender /path/to/tracks.txt --render --gpu --elements g4_00.wrl \
    --blender /Applications/Blender.app/Contents/MacOS/Blender
```

`--gpu` renders with Metal on Apple Silicon (CUDA/OptiX/HIP/oneAPI elsewhere)
and is much faster than the CPU default; it falls back to CPU with a warning
if no GPU device is found.

`--blender PATH` implies local (non-Docker) mode; `python3 -m bunchrenderer …`
is equivalent to `./bin/bunchrender …`. Movies land in `<output>_renders/`,
one MP4 per camera, alongside the `.blend`.

### Watching a long render

Output is written **directly to the final destination** (`--render-dir`,
default `<output>_renders/`), so you can watch it fill up while Blender works.
With `--format png` each frame appears as
`<render-dir>/<camera>/frame_NNNN.png` the moment it finishes — open the
folder and keep refreshing the newest file. With the **default `--format
mp4` there are no per-frame PNGs**: each camera produces a single
`<render-dir>/<camera>_0001-NNNN.mp4` that grows during rendering and is
only playable once that camera finishes (so finished cameras are watchable
while later ones still render). `--format png` gives the individual frames;
`--format both` writes the PNG frames *and* the MP4 (the MP4 is encoded from
the frames with no extra Cycles render, so it costs the same as `png`).

By default nothing is overwritten: each run tags the `.blend`, `.log`, render
directory **and every MP4 filename** with `_<YYYYmmdd-HHMMSS>_<githash>` (the
short commit hash of the bunchrenderer source, plus `-dirty` if it has
uncommitted edits), so successive runs sit side by side and each movie is
self-identifying even moved out of its folder. Pass `--no-timestamp` for
stable, clobbering names (e.g. `beam_0001-0240.mp4`) — handy when iterating
and watching a fixed path.

The terminal shows a live progress bar with frame counts, per-camera
position, elapsed time, ETA and the estimated total:

```text
[██████████··················]  38.2%  cam 3/8 xxpyp    935/2448f  elapsed 12:41  ETA 20:31  total ~33:12
```

Each camera announces its output path when it starts. Blender's raw output
is teed to `<output>.log`; pass `-v/--verbose` to stream it instead of the
bar. The `.blend` is saved *before* rendering begins, and a crashed or
interrupted render keeps everything rendered so far; only the hidden
`.bunchrenderer-*` staging folder (input data for Blender) is temporary, and
it is removed automatically.

Prefer `pip install -e .` for a git clone — the editable install tracks your
checkout, so a `git pull` takes effect immediately. A plain `pip install .`
snapshots the code: after pulling you must reinstall, or the `bunchrender`
command keeps running the old version (the CLI prints its version and source
path at startup so you can tell). `./bin/bunchrender` always runs the
checkout.

### Installing behind an internal pip index

`pip install .` (and `pip install -e .`) needs **no network at all**: the
repo ships a self-contained stdlib-only build backend, so it works even when
pip is pinned to an unreachable internal index (e.g. CERN's acc-py offsite).
If your pip config causes other trouble, `pip config debug` shows which file
pins the index (`pip config set global.index-url https://pypi.org/simple`
resets it) — or skip installing entirely; `./bin/bunchrender` runs straight
from the clone.

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

# Compare two beams (e.g. on-axis vs. an 8 mm off-axis bunch) in every view
bunchrender examples/gaussian_beam.txt examples/gaussian_beam_offset.txt \
    --render --labels "reference,off-axis"
```

By default **only the `.blend` is produced**; `--render` switches on the fully
headless render. On first use, Docker mode builds a small image
(`bunchrenderer/blender:5.0.1`, Ubuntu + the official Blender build);
`--docker-image IMAGE` substitutes any image with `blender` on its `PATH`,
and `docker build --build-arg BLENDER_VERSION=4.5.10 …` builds the bundled
Dockerfile against another Blender release. Local mode supports Blender
4.2 LTS through 5.x.

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

**Canonical-coordinate CSVs** (MAD-X / Bmad style, detected by an `s`
column): `s, x, px, y, py, z, delta` with lengths in meters, `px`/`py`
normalized to the reference momentum, `z` the longitudinal offset from the
reference particle and `delta` = Δp/p₀. The lab position becomes `s + z`,
momenta stay in units of p₀ (slopes are unit-independent; the Pz axis then
reads ≈ 1+δ), and the animation clock follows `s`. Repeated rows at the same
`s` (thin elements, markers) are dropped automatically. Finely-sliced
lattices automatically get a longer, finer-sampled movie (see *Timeline*).

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

Solids become faint additive ghosts tinted with their g4bl colors, overlaid
with thin emissive wireframes. The ghost shells are purely additive (a
transparent shader plus a soft emission, with no Fresnel/specular), so they
only add a gentle glow and **never occlude the beam** — particles stay fully
visible even on-axis inside a pipe bore or buried in many nested volumes.
Shapes wider than `--elements-max-radius` (default 10000 mm = 10 m) are dropped
so enclosure/world volumes don't swallow the scene.

**Wedge absorbers stand out.** The ordinary geometry is kept *extremely* faint
so it never competes with the beam, but any shape whose name starts with
`wedge` (case-insensitive) gets a brighter wireframe and a near-solid material
(only ~20% transparent), since the wedges are the key physics elements. With
`--reveal-elements` the wedge's opacity is windowed like the rest, so it still
whooshes past the head instead of sitting permanently in front of the beam.

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
| `beam_axial` | `Cam_beam_axial` (down the bore) | same scene from the beam axis — shows transverse rotation |
| `beam_xy` | `Cam_beam_xy` (straight down axis, perspective) | transverse (x, y) view, perspective |
| `beam_xy_ortho` | `Cam_beam_xy_ortho` (straight down axis, orthographic) | flat transverse (x, y) view |
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
to. Titles, readouts, legends and panel captions are *not* objects in the 3D
world: each camera has an `OV_<camera>` overlay scene of flat text that the
compositor alpha-combines over the frame, so text is pixel-crisp and fixed
in place while the camera moves.

Headless rendering selects the right overlay and HUD-panel set per camera
automatically. To render a camera by hand inside Blender: enable its
`HUD_<camera>` collection (the 2D sub-projection panels), and point the
second Render Layers node in the compositor at the matching `OV_<camera>`
scene.

## Timeline

```text
|-- fade-in --|------------ evolution ------------|-- hold --|-- fade-out --|
  wake from      bunch travels, phase space         final      everything
  black           evolves, cameras orbit            state      fades to black
  (0.75 s)        (--frames / --fps)                (0.5 s)    (1.5 s)
```

Tune with `--fade-in`, `--hold`, `--fade-out` (seconds; 0 disables a phase).
Cameras orbit through the entire timeline, including the hold and fades.

**The movie length scales with the input granularity.** By default the
resampling grid matches the number of steps in the tracks (memory-capped by
the particle count) and the evolution runs ~2 frames per sample, so structure
present in the input is resolvable in time: a 50-station g4bl file gives the
minimum ~13 s movie, while a finely-sliced tracking file grows the movie up
to `--max-duration` (default 180 s, fades included). Explicit `--frames` /
`--time-samples` override the automation entirely.

## Options

```text
bunchrender INPUT [INPUT2 ...] [-o out.blend]
  --labels A,B            display names for the beams (default: file names)
  --local                 use local Blender instead of Docker
  --blender PATH          Blender executable (implies --local)
  --docker-image IMAGE    alternative Docker image with `blender` on PATH
  --render                headless mode: render the animation after building
  --render-dir DIR        render output location (default <output>_renders)
  --timestamp/--no-timestamp  tag outputs (incl. MP4 names) with run
                          timestamp+commit hash so nothing clobbers (default on)
  --cameras LIST          cameras to render (default all)
  --render-frames N       render only the first N frames (0 = all)
  --format mp4|png|both   movie per camera, PNG frame folders, or both
  --gpu                   render on the GPU (Metal/CUDA/OptiX/HIP/oneAPI)
  --lq                    quick draft (12 samples, 960x540, beam cam, GPU)
  --llq                   even lower/faster draft (4 samples, 640x360, no fades)
  --views LIST            which views to build (default all)
  --elements FILE         beamline geometry: g4bl VRML (.wrl) or CSV
  --elements-max-radius MM  drop shapes wider than this (default 10000 = 10 m)
  --reveal-elements       geometry glows only around the head — brightening as it
                          approaches, staying lit as it passes, then fading once
                          it falls behind, so it whooshes past without ever
                          permanently blocking the beam
  --reveal-ahead CM       how far ahead of the head an element starts to glow
                          (default 100 = 1 m; needs --reveal-elements)
  --reveal-behind CM      how far behind the head an element stays lit before
                          fading out (default 100 = 1 m; needs --reveal-elements)
  --no-hull               skip the convex-hull envelopes
  --trails N              real-space comet tails fading over N samples (0=off)
  --fixed-zoom            hold the real-space camera at one zoom (no changes)
  --zoom-hold SEC         min time the zoom is held before it changes; level
                          changes are eased smoothly, not snapped (default 5)
  --head-depth FRAC       axial (x, y) views: depth kept behind the head, as a
                          fraction of the beamline (default 0.6); auto-deepened
                          so comet --trails always show in full
  --dead-distance CM      particles >CM behind the head (along z) are dead and
                          removed from the render (default 30; 0 = off)
  --aperture FSTOP        f-stop of the beam_xy (transverse perspective) camera,
                          focused on the bunch head; lower = wider aperture /
                          shallower focus that blurs trails & elements (def 2.0)
  --no-hud                skip the 2D sub-projection HUD panels
  --frames N --fps N      evolution length (default: auto from input granularity)
  --speed X               playback speed: <1 slower/longer, >1 faster
  --max-duration SEC      cap for the auto movie length (default 180)
  --fade-in/--hold/--fade-out SEC   timeline polish (0.75 / 0.5 / 1.5)
  --samples N             Cycles samples (default 64, denoised)
  --resolution WxH        default 1920x1080
  --max-steps N           use only the first N time steps of each input track
  --per-step              1 frame per input step (full z granularity), first
                          --per-step-seconds (default 60) of motion
  --max-particles N       cap particles kept in the scene (default 300)
  --time-samples N        resampling grid (default: auto from input granularity)
  --drift-length MM       drift used for single-point-per-particle inputs
```

## Example data

`examples/gaussian_beam.txt` is a 150-muon Gaussian bunch tracked through a
uniform focusing channel — the x–x′ and y–y′ ellipses rotate at different
rates as the bunch flies, so every projection visibly evolves.
`examples/gaussian_beam_offset.txt` is a second bunch injected 8 mm off-axis
in x (a non-trivial second beam for the multi-beam comparison). Regenerate
either (or emit a per-track CSV folder) with:

```bash
python3 examples/make_gaussian_beam.py --particles 300 --stations 80 --csv-dir tracks_csv
python3 examples/make_gaussian_beam.py --offset-x 8 --seed 7 \
    --out examples/gaussian_beam_offset.txt
```

When several beams are loaded, particle caps are split between them
(`--max-particles` total) and station axes are normalized over all beams so
the blobs are directly comparable.

`examples/beamline_solenoids.wrl` and `examples/beamline_elements.csv` are
matching beamline layouts in both supported geometry formats:

```bash
bunchrender examples/gaussian_beam.txt --render --elements examples/beamline_solenoids.wrl
```

## How it works

The CLI parses and resamples the tracks (plus any geometry) to JSON, stages it
with a scene-builder script in a temp directory, and launches
`blender -b --factory-startup --python scene_builder.py` — in Docker, the
staging directory, the output directory and the render directory are bind
mounts, so the `.blend` and renders are written straight to their final
host paths. Inside Blender, each view is a mesh
with one vertex per particle, animated through the time samples with absolute
shape keys; geometry-node modifiers turn it into renderable points and a
per-frame convex hull. Light fades are keyframed Kelvin-temperature area
lights (Blender ≥ 4.5; older versions get a blackbody-RGB approximation).
Hull deformation motion blur is disabled (its topology changes every frame);
camera and particle motion blur stay on. The animated `z` readout is a
sequence of camera-locked texts with stepped visibility keyframes, since text
bodies are not animatable in Blender.
