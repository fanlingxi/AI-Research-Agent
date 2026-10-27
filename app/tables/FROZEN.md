# Frozen F0 — d040-f0-frozen-fix6b

Code version: `verify.VERSION = "d040-f0-frozen-fix6b"`. Offline development tuning ends here. F0 outputs never enter production evidence.

Frozen thresholds:

- Physical gutter: 3 pt minimum; 0.90 source-line support.
- Hidden gutter inside a logical column: at least 3 pt of empty x projection, with at least two distinct rows represented on each side; only cells with rowspan = colspan = 1 participate. This is deliberately conservative and can reject repeated word spaces in labels.
- Structure/source text similarity: 0.65; engine box/source snapped box IoU: 0.5 (also require the engine centre inside the snapped box).
- Fake bold: identical character and font size rounded to 0.001 pt; every bbox coordinate differs by strictly less than 0.6 pt; horizontal overlap exceeds 70% of the narrower glyph; rotation differs by less than 0.1 degree. Keep the first in source order; mark other distinct IDs with duplicate_of. Duplicates are excluded from ownership and coverage and counted per table. Reused IDs still fail verification.
- Cell line grouping: vertical overlap exceeds 30% of the smaller height, or the glyph centre lies within the median line extent; x order within the line. General page grouping remains 45% overlap with maximum height ratio 2. No inferred/source spaces immediately after opening parentheses or before closing parentheses/punctuation.
- Baselines: single-line, non-row-spanning cells in the same logical row may differ by at most 0.35 times the larger median glyph height; baseline proxy is median glyph bottom.
- Wrapped prose: consecutive non-spanning cells in the same column; previous text ends in hyphen, or has at least four whitespace-separated words, reaches within 1.5 font sizes of the cell right edge, and lacks terminal punctuation (.!?;: and Chinese counterparts); following text begins lowercase or CJK. Reject as wrapped text split across rows.
- Mapped source coverage: at least 0.995; numeric/punctuation/symbol coverage: 1.0; ambiguous ownership: zero. Rotation tolerance: 0.1 degree. Cell geometry tolerance: 2 pt; candidate containment tolerance: 1 pt. Image/page aspect ratio tolerance: 0.01.
- Row-spanning cell hiding a row boundary (integrator addition before freezing): a cell with rowspan ≥ 2 and ≥ 2 text lines fails when its lines sit (baseline tolerance 0.35) on ≥ 2 different spanned rows that each carry numeric single-line value cells; header rows without numeric values are exempt. Dev: only 2024.ccl-1.3/p009-t001 (GPT-3.5 / +本文方法 merged) flips; dev verified 42/63; perturbations 210/210 rejected.
- Independent passing paths must agree on nonempty glyph ownership and header paths (empty-cell differences ignored). Disagreement fails closed. No passing path means unverified.

Integrator command (accepts any selection path; no dev-path guard):

```powershell
python -m app.tables.frozen --selection <selection.json> --paper <paper-id> --mineru-dir <saved-MinerU-directory> --output <fresh-output-directory> --render-python <render-python.exe> --paddle-python <paddle-python.exe> --paddle-cache <local-cache> --enable-model
```

The selection contains papers with paper, pdf, language and 1-based pages. Extraction includes fontname, detection, fresh rendering/crops, structured + middle MinerU JSON, one persistent PP-Structure worker, old and glyph verification paths, final rule and source spacing. The explicit enable-model switch is for the later integrator only; it was not exercised on real models during fix6.

Timings include measured paper wall time and an equal amortized share per candidate, including failures, no-detection pages, both engines, final verification and cold start. Per-engine component timings are retained separately. Saved MinerU upstream inference time is unknown. Resources are sampled paper process-tree peak RAM, process VRAM, whole-device VRAM and shared GPU memory where supported; unavailable counters remain null. These are sampled peaks, not exact OS high-water marks or individual-table peaks.

Build blinded final judging packets:

```powershell
python -m app.tables.frozen_packets --roots <paper-output-1> <paper-output-2> --output <fresh-packet-directory>
```

Public packets contain every verified table (crop, HTML, complete cell list) and every page with a caption signal or a candidate (numbered boxes). The page judge must enumerate every table by caption number and assess each box as table, complete table or not table. Identity mappings are under private/blind-map.json; distribute only packets/.

Known limitations retained: line-drawn underscore in Max_tokens; row-span ambiguity in 2024.ccl-1.2/p009-t001 (w/o rewrite / w/ fusion). No model/network calls, holdout reads or commits were made during fix6 validation. Dev artefacts and status-change evidence are under .pytest-tmp/f0-fix6/.
