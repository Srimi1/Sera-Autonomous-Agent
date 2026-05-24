"""P-99: public ship — installer manifest validation for DMG/MSI/deb."""
from sera.install.manifest import (
    ManifestError,
    Target,
    load_manifest,
    validate,
    validate_for_os,
)

__all__ = ["ManifestError", "Target", "load_manifest", "validate", "validate_for_os"]
