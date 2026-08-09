"""Deployment artifacts (launchd plists, README) plus `preflight_plist.py`
(preflight-plist unit, 2026-08-09) -- a real, tested Python module, not just
a bag of static files, so it needs this package marker for the same reason
`scripts/__init__.py` exists: `tests/test_preflight_plist.py` imports it as
`deploy.preflight_plist`."""
