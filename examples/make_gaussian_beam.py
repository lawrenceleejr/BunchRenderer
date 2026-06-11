#!/usr/bin/env python3
"""Generate an example Gaussian beam as a g4beamline-style BLTrackFile.

A bunch of muons is tracked through a uniform focusing channel
(x'' = -k^2 x), so the transverse phase-space ellipses visibly rotate as the
beam propagates -- ideal for watching phase-space evolution in BunchRenderer.

Usage:
    python examples/make_gaussian_beam.py                 # writes gaussian_beam.txt
    python examples/make_gaussian_beam.py --csv-dir tracks_csv
"""

import argparse
import math
import random
from pathlib import Path

C_MM_PER_NS = 299.792458
MUON_MASS_MEV = 105.6583755


def generate(n_particles, n_stations, length_mm, p0, seed):
    rng = random.Random(seed)
    sigma = {"x": 4.0, "xp": 2.5e-3, "y": 2.5, "yp": 1.5e-3, "dpp": 0.01}
    kx = 2 * math.pi / 2400.0   # betatron wavenumbers [rad/mm]
    ky = 2 * math.pi / 3600.0

    particles = []
    for ev in range(1, n_particles + 1):
        particles.append({
            "ev": ev,
            "x0": rng.gauss(0.0, sigma["x"]),
            "xp0": rng.gauss(0.0, sigma["xp"]),
            "y0": rng.gauss(0.0, sigma["y"]),
            "yp0": rng.gauss(0.0, sigma["yp"]),
            "p": p0 * (1.0 + rng.gauss(0.0, sigma["dpp"])),
        })

    rows = []  # station-major, like a sequence of virtual detectors
    for k in range(n_stations):
        s = length_mm * k / (n_stations - 1)
        cx, sx = math.cos(kx * s), math.sin(kx * s)
        cy, sy = math.cos(ky * s), math.sin(ky * s)
        for pt in particles:
            x = pt["x0"] * cx + pt["xp0"] / kx * sx
            xp = -pt["x0"] * kx * sx + pt["xp0"] * cx
            y = pt["y0"] * cy + pt["yp0"] / ky * sy
            yp = -pt["y0"] * ky * sy + pt["yp0"] * cy
            p = pt["p"]
            pz = p / math.sqrt(1.0 + xp * xp + yp * yp)
            px, py = xp * pz, yp * pz
            e = math.sqrt(p * p + MUON_MASS_MEV ** 2)
            beta = p / e
            t = s / (beta * C_MM_PER_NS)
            rows.append((x, y, s, px, py, pz, t, 13, pt["ev"], 1, 0, 1))
    return rows


def write_trackfile(rows, path):
    with open(path, "w") as fh:
        fh.write("#BLTrackFile example Gaussian beam in a uniform focusing channel\n")
        fh.write("#x y z Px Py Pz t PDGid EventID TrackID ParentID Weight\n")
        fh.write("#mm mm mm MeV/c MeV/c MeV/c ns - - - - -\n")
        for r in rows:
            fh.write("%.6g %.6g %.6g %.6g %.6g %.6g %.6g %d %d %d %d %d\n" % r)
    print(f"wrote {len(rows)} rows to {path}")


def write_csv_dir(rows, dirpath):
    dirpath = Path(dirpath)
    dirpath.mkdir(parents=True, exist_ok=True)
    by_event = {}
    for r in rows:
        by_event.setdefault(r[8], []).append(r)
    for ev, ev_rows in by_event.items():
        with open(dirpath / f"track_{ev:04d}.csv", "w") as fh:
            fh.write("x [mm],y [mm],z [mm],Px [MeV/c],Py [MeV/c],Pz [MeV/c],t [ns]\n")
            for r in sorted(ev_rows, key=lambda r: r[6]):
                fh.write("%.6g,%.6g,%.6g,%.6g,%.6g,%.6g,%.6g\n" % r[:7])
    print(f"wrote {len(by_event)} per-track CSV files to {dirpath}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-n", "--particles", type=int, default=150)
    ap.add_argument("--stations", type=int, default=50,
                    help="number of sampling planes along the channel")
    ap.add_argument("--length", type=float, default=3000.0, help="channel length [mm]")
    ap.add_argument("--p0", type=float, default=200.0, help="reference momentum [MeV/c]")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default=str(Path(__file__).parent / "gaussian_beam.txt"))
    ap.add_argument("--csv-dir", default=None,
                    help="also write a directory of per-track CSV files")
    args = ap.parse_args()

    rows = generate(args.particles, args.stations, args.length, args.p0, args.seed)
    write_trackfile(rows, args.out)
    if args.csv_dir:
        write_csv_dir(rows, args.csv_dir)


if __name__ == "__main__":
    main()
