#!/usr/bin/env python3
"""Regenerate references/accounting-definitions.md from references/model-schema.json.

Integration stream (A6). references/model-schema.json (stream A1) is the
authoritative 80-field schema; this render makes the accounting definitions
reference a strict projection of it, so the two files cannot drift apart:
tests/test_naming_parity.py asserts the generated file and the schema pin the
same id set (bidirectionally).

Run from the repo root:  python scripts/gen_accounting_definitions.py
Output is deterministic; commit the rendered file alongside the schema.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "references" / "model-schema.json"
OUT = ROOT / "references" / "accounting-definitions.md"

DOC_VERSION = "1.1"
SCHEMA_VERSION_REQUIRED = "1.0"

UNIT_FOR_KIND = {
    "absolute": "USDmm",
    "per_share": "USD",
    "shares_mm": "millions",
    "ratio": "fraction",
}

# derived metrics that are not ratios (schema lacks an explicit kind there)
DERIVED_UNIT_OVERRIDE = {
    "derived.fcf": "USDmm",
    "derived.net_debt": "USDmm",
}

SECTION_ORDER = ["income_statement", "balance_sheet", "cash_flow", "segments"]


def _render_field(field: dict) -> str:
    unit = UNIT_FOR_KIND.get(field.get("kind"), field.get("kind", "?"))
    parts = [f"- `{field['id']}` — {field['label']} ({unit})"]
    if field.get("derives"):
        parts.append(f"; derived: {field['derives']}")
    if field.get("sign_convention"):
        parts.append(f"; {field['sign_convention']}")
    if field.get("optional"):
        parts.append("; optional residual")
    return "".join(parts) + "\n"


def render(schema: dict) -> str:
    statements = schema["statements"]
    lines: list[str] = []
    lines.append(f"# Accounting Definitions — canonical line-item names v{DOC_VERSION}\n\n")
    lines.append(
        f"> v{DOC_VERSION} changelog: regenerated verbatim from "
        "`references/model-schema.json` (schema "
        f"v{schema.get('schema_version', SCHEMA_VERSION_REQUIRED)}, stream A1 — the "
        "authoritative 80-field schema). Ids here and ids there are now the "
        "same set; `tests/test_naming_parity.py` fails the build on any drift "
        "in either direction.\n\n"
    )
    lines.append(
        "One canonical name per line. Extraction, model, and forecast artifacts "
        "use these names verbatim (coordinate with "
        "references/model-schema.json, which pins the same names — stream A1 "
        "owns that file; this file is its rendered projection). All values USD "
        "millions (USDmm) unless the field kind says otherwise: per-share "
        "fields are USD, share counts are millions, ratios are fractions. "
        "Filings are extracted \"as reported\"; versioned mapping tables "
        "(references/segment-mapping-v1.json) translate as-reported captions "
        "to these names.\n\n"
    )

    for key in SECTION_ORDER:
        st = statements[key]
        lines.append(f"## {key}.* ({st['display']})\n\n")
        if key == "segments":
            seg_names = st["segments"]
            slugs = [s.lower() for s in seg_names]
            lines.append(
                f"Canonical segments (schema names): {', '.join(seg_names)} "
                f"— canonical series ids (lowercase slugs): {', '.join(slugs)}. "
                "forecast_bounds / mandate keys use the schema names "
                "(CASE-SENSITIVE); model series and facts use the lowercase "
                "ids below.\n\n"
            )
            lines.append(
                "Quarterly facts use the AS-REPORTED full segment names from "
                "the filing (e.g. `segments.Client Computing Group.revenue`); "
                "the deterministic model engine translates them onto the "
                "canonical ids via references/segment-mapping-v1.json "
                "(as-reported captions are pinned there per period). FY/annual "
                "facts use the canonical ids directly. Where a period does not "
                "separately quantify a canonical segment, the residual is kept "
                "in the optional `segments.all_other_*` fields and the "
                "canonical series point stays absent (no interpolation, no "
                "carry-forward).\n\n"
            )
            for field in st["per_segment_fields"]:
                for name, slug in zip(seg_names, slugs):
                    lines.append(
                        _render_field({
                            "id": f"segments.{slug}.{field['suffix']}",
                            "label": f"{field['label']} ({name})",
                            "kind": field["kind"],
                            "derives": field.get("derives"),
                        })
                    )
            for resid in st.get("residuals", []):
                lines.append(_render_field(resid))
        else:
            for field in st["fields"]:
                lines.append(_render_field(field))
        lines.append("\n")

    lines.append("## derived.* (computed by the deterministic engines)\n\n")
    for metric in schema.get("derived_metrics", []):
        unit = DERIVED_UNIT_OVERRIDE.get(metric["id"], UNIT_FOR_KIND["ratio"])
        lines.append(f"- `{metric['id']}` — {metric['label']} ({unit})"
                     f"; expr: {metric['expr']}\n")
    lines.append(
        "\n## ratio conventions\n\n"
        "- Periods label as `YYYY-QN` (quarterly) or `FY2024`/annual `YYYY`.\n"
        "- YoY = vs. same period prior year; QoQ = vs. prior quarter "
        "(A1 engine computes).\n"
    )
    return "".join(lines)


def main() -> int:
    schema = json.loads(SCHEMA.read_text("utf-8"))
    OUT.write_text(render(schema), encoding="utf-8")
    print(f"wrote {OUT} from {SCHEMA} (schema v{schema.get('schema_version')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
