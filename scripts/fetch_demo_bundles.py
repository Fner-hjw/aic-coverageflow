"""Fetch the demo EDGAR bundles (Intel 12 docs, AMD held-out ~10 docs).

Run once during development to snapshot the frozen bundles into
data/intel-bundle/ and data/amd-bundle/; the test suite never touches the
network. All accessions were verified against the EDGAR submissions API.
"""

from pathlib import Path

from coverageflow.bundle import DocSpec, build_bundle

REPO = Path(__file__).resolve().parent.parent

INTEL = [
    DocSpec("intc-10k-fy2024", "0000050863-25-000009", "10-K", "2025-01-31", "2024-12-28", ticker="INTC"),
    DocSpec("intc-10k-fy2025", "0000050863-26-000011", "10-K", "2026-01-23", "2025-12-27", ticker="INTC"),
    DocSpec("intc-10q-2025q1", "0000050863-25-000074", "10-Q", "2025-04-25", "2025-03-29", ticker="INTC"),
    DocSpec("intc-10q-2025q2", "0000050863-25-000109", "10-Q", "2025-07-24", "2025-06-28", ticker="INTC"),
    DocSpec("intc-10q-2025q3", "0000050863-25-000179", "10-Q", "2025-11-06", "2025-09-27", ticker="INTC"),
    DocSpec("intc-10q-2026q1", "0000050863-26-000079", "10-Q", "2026-04-24", "2026-03-28", ticker="INTC"),
    DocSpec("intc-10q-2026q2", "0000050863-26-000157", "10-Q", "2026-07-24", "2026-06-27", ticker="INTC"),
    DocSpec("intc-10q-2026q3", "0000050863-26-000191", "10-Q", "2026-10-30", "2026-09-26", ticker="INTC"),
    DocSpec("intc-8k-2024-08-01", "0000050863-24-000122", "8-K", "2024-08-01", "2024-08-01", ticker="INTC"),  # restructuring + dividend
    DocSpec("intc-8k-2024-12-03", "0000050863-24-000173", "8-K", "2024-12-03", "2024-12-01", ticker="INTC"),  # CEO transition
    DocSpec("intc-8k-2025-09-15", "0000050863-25-000153", "8-K", "2025-09-15", "2025-09-12", ticker="INTC"),  # item 2.01 disposition
    DocSpec("intc-8k-2026-10-29", "0000050863-26-000188", "8-K", "2026-10-29", "2026-10-29", ticker="INTC"),  # Q3'26 earnings release
]

# Held-out-company bundle. Includes every quarterly 10-Q of FY2025-2026,
# both 10-Ks, the ZT Systems acquisition-completion 8-K (item 2.01) and the
# Q3'26 earnings 8-K (item 2.02). Accessions/dates verified via submissions API.
AMD = [
    DocSpec("amd-10k-fy2024", "0000002488-25-000012", "10-K", "2025-02-05", "2024-12-28", ticker="AMD"),
    DocSpec("amd-10k-fy2025", "0000002488-26-000018", "10-K", "2026-02-04", "2025-12-27", ticker="AMD"),
    DocSpec("amd-10q-2025q1", "0000002488-25-000047", "10-Q", "2025-05-07", "2025-03-29", ticker="AMD"),
    DocSpec("amd-10q-2025q2", "0000002488-25-000108", "10-Q", "2025-08-06", "2025-06-28", ticker="AMD"),
    DocSpec("amd-10q-2025q3", "0000002488-25-000166", "10-Q", "2025-11-05", "2025-09-27", ticker="AMD"),
    DocSpec("amd-10q-2026q1", "0000002488-26-000076", "10-Q", "2026-05-06", "2026-03-28", ticker="AMD"),
    DocSpec("amd-10q-2026q2", "0000002488-26-000123", "10-Q", "2026-08-05", "2026-06-27", ticker="AMD"),
    DocSpec("amd-10q-2026q3", "0000002488-26-000191", "10-Q", "2026-11-04", "2026-09-26", ticker="AMD"),
    DocSpec("amd-8k-2025-10-27", "0001193125-25-250803", "8-K", "2025-10-27", "2025-10-27", ticker="AMD"),  # item 2.01: ZT Systems completion
    DocSpec("amd-8k-2026-11-03", "0000002488-26-000188", "8-K", "2026-11-03", "2026-11-03", ticker="AMD"),  # item 2.02: Q3'26 earnings
]


def main() -> None:
    for cik, specs, dest in [
        ("0000050863", INTEL, REPO / "data" / "intel-bundle"),
        ("0000002488", AMD, REPO / "data" / "amd-bundle"),
    ]:
        manifest = build_bundle(cik, specs, dest)
        manifest.verify(dest)
        print(
            f"{manifest.bundle_id}: {len(manifest.documents)} docs, "
            f"cutoff {manifest.as_of_cutoff}, bundle_sha {manifest.bundle_sha256[:16]}..."
        )
        for d in manifest.documents:
            print(f"  {d.doc_id:20s} {d.form:4s} filed {d.filed} period {d.period} {d.bytes:>10,} bytes")


if __name__ == "__main__":
    main()
