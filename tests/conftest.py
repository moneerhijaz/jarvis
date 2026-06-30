import os
import sys
from pathlib import Path

# Make the package importable when running pytest from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Isolate tests from the developer's config/local.yaml: point JARVIS_CONFIG at a
# path that doesn't exist, so load_settings() uses only packaged defaults + each
# test's explicit overrides (deterministic regardless of the local machine).
os.environ["JARVIS_CONFIG"] = str(Path(__file__).resolve().parent / "_no_local_config.yaml")
