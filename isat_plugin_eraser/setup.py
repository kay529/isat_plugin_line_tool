# -*- coding: utf-8 -*-
"""Packaging metadata for the ISAT eraser plugin.

License: MIT (this plugin). It is an independent plugin for
ISAT_with_segment_anything (Apache-2.0, by yatengLG); no ISAT source is
redistributed here. See README.md and LICENSE for details.
"""
from setuptools import find_packages, setup

setup(
    name="isat-plugin-eraser",
    version="1.0.0",
    author="ISAT eraser contributors",
    description=(
        "ISAT plugin: erase a piece out of an annotation that already exists. "
        "Press on it, drag over the part you want gone, release -- like a "
        "brush in Label Studio. Keeps the annotation's category and group."
    ),
    long_description=open("README.md", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    license="MIT",
    packages=find_packages(),
    python_requires=">=3.8",
    install_requires=["shapely", "numpy", "opencv-python"],
    classifiers=[
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Topic :: Scientific/Engineering :: Image Processing",
        "Environment :: X11 Applications :: Qt",
    ],
    keywords=[
        "isat", "annotation", "segmentation", "polygon", "labeling", "erase",
    ],
    entry_points={
        "isat.plugins": [
            "eraser = isat_plugin_eraser:Plugin",
        ],
    },
)
