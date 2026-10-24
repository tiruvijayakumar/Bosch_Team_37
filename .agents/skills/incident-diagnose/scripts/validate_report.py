#!/usr/bin/env python3
"""Validate a diagnosis JSON against the organisers' schema.
Usage: python validate_report.py <report.json> [schema.json]
"""
import json
import sys
from pathlib import Path

from jsonschema import validators

here = Path(__file__).resolve().parent
report_path = Path(sys.argv[1])
schema_path = Path(sys.argv[2]) if len(sys.argv) > 2 else here.parent / "references" / "schema.json"

schema = json.loads(schema_path.read_text())
if schema.get("x-placeholder"):
    print("SCHEMA IS STILL THE PLACEHOLDER: paste the organisers' JSON schema into", schema_path)
    sys.exit(2)

report = json.loads(report_path.read_text())
validator = validators.validator_for(schema)(schema)
errors = sorted(validator.iter_errors(report), key=lambda e: list(e.path))
if errors:
    for e in errors:
        loc = "/".join(str(p) for p in e.path) or "<root>"
        print(f"INVALID at {loc}: {e.message}")
    sys.exit(1)
print("VALID" + (" (against PROVISIONAL schema - confirm against the organisers' schema)" if schema.get("x-provisional") else ""))
