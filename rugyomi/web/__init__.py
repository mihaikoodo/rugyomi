"""The read-only site (RGY-014). Needs the optional dependencies: `pip install -e ".[web]"`."""
from .app import create_app, default_db

__all__ = ["create_app", "default_db"]
