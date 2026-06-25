"""Version information for the coding-agents package.

Reads from installed package metadata. Falls back to "unknown" if the
package is not installed (e.g. running from source without pip install).
"""

from __future__ import annotations

import importlib.metadata

try:
    __version__ = importlib.metadata.version("coding-agents")
except importlib.metadata.PackageNotFoundError:
    __version__ = "unknown"
