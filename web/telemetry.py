"""Serve the canonical snapshot written by apu_telemetry."""
import json
from pathlib import Path

BASE_PATH = Path('/run/apu_telemetry.json')


def read_telemetry(base_path=BASE_PATH):
    try:
        with Path(base_path).open() as source:
            data = json.load(source)
        if not isinstance(data, dict):
            raise ValueError('telemetry must be an object')
        return data
    except (OSError, ValueError) as error:
        return {'error': str(error)}
