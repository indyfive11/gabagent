"""Image generation plugin — the Gab/Aria `/v1/images/generations` capability.

GA-lane (this package): the `generate_image` tool, the API call + local file write, the display descriptor,
and local-file GC. The cross-host display seam (routing a descriptor to a screen by room-locality) is VAC's
lane and rides a separate transport. Importing `.tool` registers the tool with the global registry.
"""
from __future__ import annotations

from .generate import ImageDescriptor, generate_images, png_dimensions
from .gc import gc_old_images

__all__ = [
    "ImageDescriptor",
    "generate_images",
    "png_dimensions",
    "gc_old_images",
]
