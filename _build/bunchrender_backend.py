"""Self-contained PEP 517/660 build backend (stdlib only).

Why this exists: machines pinned to an internal pip index (e.g. CERN acc-py)
cannot download setuptools into pip's isolated build environment when offsite,
so a plain `pip install .` fails before our code is even looked at. This
backend declares no build requirements and builds the wheel with the standard
library alone, so installation never touches the network.
"""

import base64
import hashlib
import os
import tarfile
import zipfile

NAME = "bunchrenderer"
VERSION = "0.2.0"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

METADATA = """\
Metadata-Version: 2.1
Name: bunchrenderer
Version: {version}
Summary: Render particle-beam track data (g4beamline / CSV) as animated, studio-lit Blender scenes.
Requires-Python: >=3.9
""".format(version=VERSION)

WHEEL_META = """\
Wheel-Version: 1.0
Generator: bunchrender_backend
Root-Is-Purelib: true
Tag: py3-none-any
"""

ENTRY_POINTS = """\
[console_scripts]
bunchrender = bunchrenderer.cli:main
"""

PACKAGE_FILES = ("*.py", "blender/*.py", "docker/Dockerfile",
                 "fonts/*.ttf", "fonts/*.txt")


def _package_files():
    """(archive_name, source_path) for everything that ships in the wheel."""
    import glob
    out = []
    for pattern in PACKAGE_FILES:
        for path in sorted(glob.glob(os.path.join(ROOT, NAME, pattern))):
            rel = os.path.relpath(path, ROOT)
            out.append((rel.replace(os.sep, "/"), path))
    return out


def _record_hash(data):
    digest = hashlib.sha256(data).digest()
    return "sha256=" + base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _write_wheel(wheel_directory, contents):
    """contents: list of (archive_name, bytes). Adds dist-info + RECORD."""
    dist_info = f"{NAME}-{VERSION}.dist-info"
    contents = list(contents) + [
        (f"{dist_info}/METADATA", METADATA.encode()),
        (f"{dist_info}/WHEEL", WHEEL_META.encode()),
        (f"{dist_info}/entry_points.txt", ENTRY_POINTS.encode()),
    ]
    record_lines = [f"{n},{_record_hash(d)},{len(d)}" for n, d in contents]
    record_lines.append(f"{dist_info}/RECORD,,")
    contents.append((f"{dist_info}/RECORD",
                     ("\n".join(record_lines) + "\n").encode()))

    fname = f"{NAME}-{VERSION}-py3-none-any.whl"
    path = os.path.join(wheel_directory, fname)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in contents:
            zf.writestr(zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0)), data)
    return fname


# -- PEP 517 hooks -----------------------------------------------------------

def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    contents = []
    for arcname, path in _package_files():
        with open(path, "rb") as fh:
            contents.append((arcname, fh.read()))
    return _write_wheel(wheel_directory, contents)


def build_sdist(sdist_directory, config_settings=None):
    fname = f"{NAME}-{VERSION}.tar.gz"
    base = f"{NAME}-{VERSION}"
    extra = ["pyproject.toml", "setup.py", "README.md",
             "_build/bunchrender_backend.py"]
    with tarfile.open(os.path.join(sdist_directory, fname), "w:gz") as tf:
        for arcname, path in _package_files():
            tf.add(path, arcname=f"{base}/{arcname}")
        for rel in extra:
            path = os.path.join(ROOT, rel)
            if os.path.exists(path):
                tf.add(path, arcname=f"{base}/{rel}")
    return fname


# -- PEP 660 hook (pip install -e .) -----------------------------------------

def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    pth = (ROOT + "\n").encode()
    return _write_wheel(wheel_directory, [(f"__editable__.{NAME}.pth", pth)])
