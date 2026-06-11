"""Compatibility shim so `pip install --no-build-isolation .` works even when
the ambient setuptools predates pyproject.toml [project] metadata (PEP 621).

This matters on machines whose pip is pinned to an unreachable internal
package index (e.g. CERN acc-py offsite): build isolation cannot download
setuptools there, and --no-build-isolation must work with whatever setuptools
is already installed. Metadata here mirrors pyproject.toml.
"""

from setuptools import setup

setup(
    name="bunchrenderer",
    version="0.1.0",
    description=("Render particle-beam track data (g4beamline / CSV) as "
                 "animated, studio-lit Blender scenes."),
    python_requires=">=3.9",
    packages=["bunchrenderer"],
    package_data={"bunchrenderer": ["blender/*.py", "docker/Dockerfile"]},
    include_package_data=True,
    entry_points={"console_scripts": ["bunchrender=bunchrenderer.cli:main"]},
)
