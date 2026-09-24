"""Paths resolved independently of the process working directory."""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
