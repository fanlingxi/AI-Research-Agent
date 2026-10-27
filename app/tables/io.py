"""Small strict JSON helpers shared by the offline commands."""

import json
from pathlib import Path


def read(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject(value):
        raise ValueError(f"Nonfinite JSON number: {value}")

    return json.loads(
        Path(path).read_text(encoding="utf-8"), object_pairs_hook=pairs, parse_constant=reject
    )


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )
