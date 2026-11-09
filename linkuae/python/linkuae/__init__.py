"""Python client for LinkUAE (shared-memory external control)."""
from .link import Link, Frame, StaleFrame, EmulatorGone, emulator_present, qpc
from . import protocol
