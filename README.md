# Acculligence Think Tank Monitor v2

This replaces v1.

## Architecture
1. `build_acquisition_map.py` discovers and validates official routes for every source.
2. It writes `config/acquisition_map.json`.
3. `collector.py` uses only that validated map.
4. Google News is not accepted as a primary route.
5. Sources without a validated official route are marked `blocked`, not silently searched elsewhere.

## Approved keywords
Saudi, Riyadh, Jeddah, Aramco, bin Salman, السعودية, الرياض, جدة, أرامكو

## Matching
ANY keyword in the publication title or cleaned main body.

## First run
Use GitHub Actions:
- start: `2026-07-25`
- end: `2026-07-31`
- rebuild_map: `yes`

## Outputs
- `config/acquisition_map.json`
- `output/articles_YYYY-MM-DD_YYYY-MM-DD.csv`
- `output/audit_YYYY-MM-DD_YYYY-MM-DD.csv`

The audit file explicitly distinguishes:
- `complete`: official route validated and processed
- `blocked`: no official route was validated
