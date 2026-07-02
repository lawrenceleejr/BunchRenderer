"""Parsing and resampling of particle-track data.

Supported inputs:

* a g4beamline ``BLTrackFile`` ASCII file (one file containing many tracks,
  grouped by EventID/TrackID), e.g. the output of a ``trackfile`` command or a
  ``virtualdetector`` writing in ``ascii`` format;
* a directory of CSV files, one file per particle track.

Internal units follow the g4beamline convention: positions in mm, momenta in
MeV/c, time in ns.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path

C_MM_PER_NS = 299.792458  # speed of light

# Masses [MeV/c^2] for the PDG ids most likely to show up in beam files.
PDG_MASS_MEV = {
    11: 0.51099895,
    13: 105.6583755,
    211: 139.57039,
    321: 493.677,
    2212: 938.27208816,
    2112: 939.5654205,
    22: 0.0,
}
DEFAULT_MASS_MEV = PDG_MASS_MEV[13]

# Column order of a standard BLTrackFile when no header line is found.
G4BL_DEFAULT_COLUMNS = [
    "x", "y", "z", "px", "py", "pz", "t",
    "pdgid", "eventid", "trackid", "parentid", "weight",
]

CSV_ALIASES = {
    "x": "x", "y": "y", "z": "z",
    "px": "px", "py": "py", "pz": "pz",
    "t": "t", "time": "t",
    "xp": "xp", "x'": "xp", "yp": "yp", "y'": "yp",
    "s": "s", "delta": "delta", "dpp": "delta", "dp/p": "delta", "de": "delta",
    "pdgid": "pdgid", "pdg": "pdgid",
}


class TrackError(ValueError):
    """Raised when input track data cannot be interpreted."""


def _mass_for(pdgid):
    return PDG_MASS_MEV.get(abs(int(pdgid)), DEFAULT_MASS_MEV)


def _normalize_name(name):
    """'Px [MeV/c]' -> 'px'."""
    name = name.strip().lower()
    for sep in ("[", "("):
        if sep in name:
            name = name.split(sep)[0].strip()
    return CSV_ALIASES.get(name)


def _new_track(pdgid=13):
    return {"t": [], "x": [], "y": [], "z": [],
            "px": [], "py": [], "pz": [], "pdgid": pdgid}


# --------------------------------------------------------------------------
# g4beamline BLTrackFile

def _read_g4bl(path):
    """Read a BLTrackFile; returns a list of tracks grouped by Event/Track id."""
    cols = None
    rows = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                toks = [t.lower() for t in line.lstrip("#").replace(",", " ").split()]
                # The column-name comment line contains at least x y z px.
                if {"x", "y", "z", "px"} <= set(toks):
                    cols = toks
                continue
            rows.append(line.split())
    if not rows:
        raise TrackError(f"no data rows found in {path}")
    if cols is None:
        cols = G4BL_DEFAULT_COLUMNS
    idx = {name: i for i, name in enumerate(cols)}
    for required in ("x", "y", "z"):
        if required not in idx:
            raise TrackError(f"{path}: missing required column '{required}'")

    def get(row, name, default=0.0):
        i = idx.get(name)
        if i is None or i >= len(row):
            return default
        return float(row[i])

    groups = {}
    order = []
    for n, row in enumerate(rows):
        if "eventid" in idx:
            key = (int(get(row, "eventid")), int(get(row, "trackid", 1)))
        else:
            key = n  # no ids: every row is its own (single-point) particle
        tr = groups.get(key)
        if tr is None:
            tr = _new_track(pdgid=int(get(row, "pdgid", 13)))
            groups[key] = tr
            order.append(key)
        tr["t"].append(get(row, "t"))
        tr["x"].append(get(row, "x"))
        tr["y"].append(get(row, "y"))
        tr["z"].append(get(row, "z"))
        tr["px"].append(get(row, "px"))
        tr["py"].append(get(row, "py"))
        tr["pz"].append(get(row, "pz", 1.0))
    return [groups[k] for k in order]


# --------------------------------------------------------------------------
# Directory of per-track CSV files

def _canonical_track(cols, npts):
    """Accelerator canonical coordinates (MAD-X / Bmad style):

        s, x, px, y, py, z, delta

    with lengths in meters, ``px``/``py`` normalized to the reference
    momentum, ``z`` the longitudinal offset from the reference particle and
    ``delta`` = dp/p0. Detected by the presence of an ``s`` column. The lab
    longitudinal position becomes s+z [mm]; momenta are kept in units of p0
    (slopes px/pz are unit-independent); time is synthesized from s assuming
    beta ~ 1 so the animation parameter is the position along the lattice.
    """
    zero = [0.0] * npts
    s = cols["s"]
    x, y = cols.get("x", zero), cols.get("y", zero)
    zoff = cols.get("z", zero)
    delta = cols.get("delta", zero)
    pxn, pyn = cols.get("px", zero), cols.get("py", zero)
    tr = _new_track()
    for i in range(npts):
        one = 1.0 + delta[i]
        pz = math.sqrt(max(one * one - pxn[i] ** 2 - pyn[i] ** 2, 1e-12))
        tr["x"].append(x[i] * 1000.0)
        tr["y"].append(y[i] * 1000.0)
        tr["z"].append((s[i] + zoff[i]) * 1000.0)
        tr["px"].append(pxn[i])
        tr["py"].append(pyn[i])
        tr["pz"].append(pz)
        tr["t"].append(s[i] * 1000.0 / C_MM_PER_NS)
    return tr


def _read_csv_track(path):
    with open(path, newline="") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t ")
        except csv.Error:
            dialect = csv.excel
        rows = [r for r in csv.reader(fh, dialect)
                if r and not r[0].lstrip().startswith("#")]
    if not rows:
        raise TrackError(f"{path}: empty CSV")

    header = None
    try:
        [float(c) for c in rows[0] if c.strip()]
    except ValueError:
        header = rows[0]
        rows = rows[1:]
    if not rows:
        raise TrackError(f"{path}: CSV has a header but no data")

    ncol = len(rows[0])
    if header is not None:
        names = [_normalize_name(c) for c in header]
    else:
        # Positional fallback for headerless files.
        by_count = {
            3: ["x", "y", "z"],
            4: ["x", "y", "z", "t"],
            6: ["x", "y", "z", "px", "py", "pz"],
            7: ["x", "y", "z", "px", "py", "pz", "t"],
        }
        names = by_count.get(ncol)
        if names is None:
            raise TrackError(
                f"{path}: headerless CSV with {ncol} columns; expected 3, 4, 6 or 7 "
                "(x y z [px py pz] [t]) or a header row naming the columns")

    idx = {n: i for i, n in enumerate(names) if n}
    canonical = "s" in idx  # MAD-X / Bmad style coordinates along the lattice
    if not canonical:
        for required in ("x", "y", "z"):
            if required not in idx:
                raise TrackError(f"{path}: could not identify column '{required}'")

    cols = {n: [] for n in idx}
    for row in rows:
        if len(row) < ncol:
            continue
        for n, i in idx.items():
            cols[n].append(float(row[i]))
    npts = len(cols["s" if canonical else "x"])
    if npts == 0:
        raise TrackError(f"{path}: no usable rows")
    if canonical:
        return _canonical_track(cols, npts)

    tr = _new_track()
    tr["x"], tr["y"], tr["z"] = cols["x"], cols["y"], cols["z"]
    if "pdgid" in cols:
        tr["pdgid"] = int(cols["pdgid"][0])

    if "px" in cols and "py" in cols and "pz" in cols:
        tr["px"], tr["py"], tr["pz"] = cols["px"], cols["py"], cols["pz"]
    elif "xp" in cols and "yp" in cols:
        # Divergences in mrad; the absolute momentum scale is arbitrary but
        # consistent, so phase-space angles px/pz are preserved.
        tr["pz"] = [1.0] * npts
        tr["px"] = [v * 1e-3 for v in cols["xp"]]
        tr["py"] = [v * 1e-3 for v in cols["yp"]]
    else:
        # Derive the direction of flight from finite differences.
        for i in range(npts):
            j = min(i, npts - 2)
            if npts == 1:
                d = (0.0, 0.0, 1.0)
            else:
                d = (cols["x"][j + 1] - cols["x"][j],
                     cols["y"][j + 1] - cols["y"][j],
                     cols["z"][j + 1] - cols["z"][j])
            norm = math.sqrt(sum(v * v for v in d)) or 1.0
            tr["px"].append(d[0] / norm)
            tr["py"].append(d[1] / norm)
            tr["pz"].append(d[2] / norm)

    if "t" in cols:
        tr["t"] = cols["t"]
    else:
        # Synthesize time from path length, assuming beta ~ 1.
        t, tlist = 0.0, [0.0]
        for i in range(1, npts):
            step = math.sqrt((tr["x"][i] - tr["x"][i - 1]) ** 2 +
                             (tr["y"][i] - tr["y"][i - 1]) ** 2 +
                             (tr["z"][i] - tr["z"][i - 1]) ** 2)
            t += step / C_MM_PER_NS
            tlist.append(t)
        tr["t"] = tlist
    return tr


def _read_csv_dir(path):
    files = sorted(p for p in Path(path).iterdir()
                   if p.suffix.lower() in (".csv", ".txt") and p.is_file())
    if not files:
        raise TrackError(f"no .csv/.txt track files found in {path}")
    return [_read_csv_track(p) for p in files]


# --------------------------------------------------------------------------
# Post-processing

def _retime_by_pathlength(tr):
    t, tlist = 0.0, [0.0]
    for i in range(1, len(tr["x"])):
        step = math.sqrt((tr["x"][i] - tr["x"][i - 1]) ** 2 +
                         (tr["y"][i] - tr["y"][i - 1]) ** 2 +
                         (tr["z"][i] - tr["z"][i - 1]) ** 2)
        t += step / C_MM_PER_NS
        tlist.append(t)
    tr["t"] = tlist


def _expand_single_point(tr, drift_length):
    """Turn a one-point track into a straight drift along its momentum."""
    p = math.sqrt(tr["px"][0] ** 2 + tr["py"][0] ** 2 + tr["pz"][0] ** 2)
    if p <= 0:
        return
    m = _mass_for(tr["pdgid"])
    beta = p / math.sqrt(p * p + m * m)
    u = (tr["px"][0] / p, tr["py"][0] / p, tr["pz"][0] / p)
    dt = drift_length / (beta * C_MM_PER_NS)
    tr["x"].append(tr["x"][0] + u[0] * drift_length)
    tr["y"].append(tr["y"][0] + u[1] * drift_length)
    tr["z"].append(tr["z"][0] + u[2] * drift_length)
    tr["px"].append(tr["px"][0])
    tr["py"].append(tr["py"][0])
    tr["pz"].append(tr["pz"][0])
    tr["t"].append(tr["t"][0] + dt)


def _sort_and_dedupe(tr):
    order = sorted(range(len(tr["t"])), key=lambda i: tr["t"][i])
    keep = []
    last_t = None
    for i in order:
        if last_t is not None and tr["t"][i] <= last_t:
            continue
        keep.append(i)
        last_t = tr["t"][i]
    if len(keep) < 2 and len(order) >= 2:
        keep = order  # all timestamps equal; keep points, fix time later
    for k in ("t", "x", "y", "z", "px", "py", "pz"):
        tr[k] = [tr[k][i] for i in keep]


def load_tracks(path, drift_length=2000.0):
    """Load tracks from a g4beamline file or a directory of CSVs.

    Single-point tracks (e.g. a beam file sampled at one plane) are expanded
    into a ballistic drift of ``drift_length`` mm along their momentum.
    """
    path = Path(path)
    if not path.exists():
        raise TrackError(f"input not found: {path}")
    if path.is_dir():
        tracks = _read_csv_dir(path)
    else:
        with open(path) as fh:
            first = fh.readline()
        if first.startswith("#BLTrackFile") or first.lstrip().startswith("#"):
            tracks = _read_g4bl(path)
        elif "," in first or ";" in first:
            tracks = [_read_csv_track(path)]
        else:
            tracks = _read_g4bl(path)

    for tr in tracks:
        _sort_and_dedupe(tr)
        if len(tr["t"]) == 1:
            _expand_single_point(tr, drift_length)
        if len(tr["t"]) >= 2 and tr["t"][-1] <= tr["t"][0]:
            _retime_by_pathlength(tr)
    tracks = [tr for tr in tracks if len(tr["t"]) >= 2]
    if not tracks:
        raise TrackError("no usable tracks (need momenta or at least two points per track)")
    return tracks


def time_range(tracks):
    """(t_start, t_end) covered by a set of tracks."""
    return (min(tr["t"][0] for tr in tracks),
            max(tr["t"][-1] for tr in tracks))


def _propagation_end_time(tr):
    """Time at which a track stops propagating: the timestamp of its last
    point that still moves. A particle that is lost/absorbed (its track ends)
    or whose trailing rows repeat the same position stops contributing after
    this time."""
    eps2 = 1e-8  # mm^2; frozen/clamped rows are bit-identical -> distance 0
    i = len(tr["t"]) - 1
    while i > 0:
        dx = tr["x"][i] - tr["x"][i - 1]
        dy = tr["y"][i] - tr["y"][i - 1]
        dz = tr["z"][i] - tr["z"][i - 1]
        if dx * dx + dy * dy + dz * dz > eps2:
            break
        i -= 1
    return tr["t"][i]


def resample(tracks, n_samples=100, max_particles=300, t_range=None):
    """Resample all tracks onto a common, uniform time grid.

    Returns a JSON-serializable bundle:
    ``times`` [S], ``pos`` [S][N][3] (mm), ``mom`` [S][N][3] (MeV/c), and
    ``span`` [N] of ``[s_start, s_stop]`` sample indices over which each
    particle is still propagating (outside this range it is frozen/lost and
    should be excluded from the convex hull).
    ``t_range`` overrides the grid span (used to put several beams on one
    shared clock).
    """
    if len(tracks) > max_particles:
        stride = (len(tracks) + max_particles - 1) // max_particles
        tracks = tracks[::stride]

    t0, t1 = t_range if t_range is not None else time_range(tracks)
    if t1 <= t0:
        raise TrackError("track data has zero time span; cannot animate")
    times = [t0 + (t1 - t0) * s / (n_samples - 1) for s in range(n_samples)]
    tol = 0.5 * (t1 - t0) / (n_samples - 1)  # half a sample, anti-flicker

    n = len(tracks)
    pos = [[None] * n for _ in range(n_samples)]
    mom = [[None] * n for _ in range(n_samples)]
    span = []
    for i, tr in enumerate(tracks):
        tt = tr["t"]
        j = 0
        for s, t in enumerate(times):
            while j < len(tt) - 2 and tt[j + 1] <= t:
                j += 1
            ta, tb = tt[j], tt[j + 1]
            f = 0.0 if tb <= ta else (t - ta) / (tb - ta)
            f = min(1.0, max(0.0, f))
            pos[s][i] = [round(tr[k][j] + f * (tr[k][j + 1] - tr[k][j]), 4)
                         for k in ("x", "y", "z")]
            mom[s][i] = [round(tr[k][j] + f * (tr[k][j + 1] - tr[k][j]), 6)
                         for k in ("px", "py", "pz")]
        t_begin, t_stop = tt[0], _propagation_end_time(tr)
        s_start = next((s for s, t in enumerate(times) if t >= t_begin - tol), 0)
        s_stop = next((s for s in range(n_samples - 1, -1, -1)
                       if times[s] <= t_stop + tol), n_samples - 1)
        span.append([s_start, max(s_start, s_stop)])

    return {
        "meta": {
            "n_particles": n,
            "n_samples": n_samples,
            "t_start_ns": round(t0, 4),
            "t_end_ns": round(t1, 4),
            "pdgid": [tr["pdgid"] for tr in tracks],
            "units": {"pos": "mm", "mom": "MeV/c", "t": "ns"},
        },
        "times": [round(t, 5) for t in times],
        "pos": pos,
        "mom": mom,
        "span": span,
    }
