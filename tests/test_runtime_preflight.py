from pathlib import Path

import pytest

from product_scraping_agent.runtime_preflight import run_runtime_preflight, write_preflight_report


@pytest.mark.asyncio
async def test_runtime_preflight_reports_output_root(tmp_path: Path):
    report = await run_runtime_preflight(output_root=tmp_path, check_browser_launch=False)
    data = report.as_dict()
    assert "checks" in data
    assert any(c["name"] == "output_root:writable" for c in data["checks"])


def test_write_preflight_report(tmp_path: Path):
    class DummyReport:
        def as_dict(self):
            return {"status": "ok", "checks": []}

    path = tmp_path / "runtime_preflight.json"
    write_preflight_report(DummyReport(), path)
    assert path.exists()
    assert "ok" in path.read_text(encoding="utf-8")
