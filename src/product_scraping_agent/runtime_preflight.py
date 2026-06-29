"""Runtime/environment preflight checks for product scraping.

The checks are intentionally explicit so setup failures are reported before a
long batch run starts. The module does not scrape product URLs.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config, get_config


@dataclass
class PreflightCheck:
    name: str
    status: str
    message: str = ""
    remediation: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def warning(self) -> bool:
        return self.status == "warning"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "remediation": self.remediation,
            "details": self.details,
        }


@dataclass
class RuntimePreflightReport:
    status: str
    checks: list[PreflightCheck]

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ok": self.ok,
            "checks": [c.as_dict() for c in self.checks],
            "failed_checks": [c.name for c in self.checks if c.status == "failed"],
            "warning_checks": [c.name for c in self.checks if c.status == "warning"],
        }


def _module_check(module_name: str, *, remediation: str = "") -> PreflightCheck:
    spec = importlib.util.find_spec(module_name)
    if spec is None:
        return PreflightCheck(
            name=f"module:{module_name}",
            status="failed",
            message=f"Python module {module_name!r} is not importable.",
            remediation=remediation or "Install project dependencies with pdm install --prod.",
        )
    return PreflightCheck(
        name=f"module:{module_name}",
        status="ok",
        message=f"Python module {module_name!r} is importable.",
        details={"origin": str(spec.origin or "")},
    )


def _output_root_check(output_root: Path) -> PreflightCheck:
    try:
        output_root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=output_root, delete=True) as f:
            f.write("ok")
            f.flush()
        return PreflightCheck(
            name="output_root:writable",
            status="ok",
            message="Output root is writable.",
            details={"output_root": str(output_root)},
        )
    except Exception as exc:
        return PreflightCheck(
            name="output_root:writable",
            status="failed",
            message=f"Output root is not writable: {type(exc).__name__}: {exc}",
            remediation="Use a writable --output-root path or fix AzureML filesystem permissions.",
            details={"output_root": str(output_root)},
        )


def _llm_config_check(config: Config) -> PreflightCheck:
    if not config.llm_enabled:
        return PreflightCheck(
            name="llm:config",
            status="warning",
            message="LLM is disabled. Deterministic fallback mode will run with degraded evidence quality.",
            remediation="Set PCA_LLM_ENABLED=true and provide gateway credentials for production runs.",
        )
    missing: list[str] = []
    if not config.llm_api_key:
        missing.append("PCA_LLM_API_KEY")
    if not config.llm_endpoint:
        missing.append("PCA_LLM_ENDPOINT")
    if not config.llm_deployment:
        missing.append("PCA_LLM_DEPLOYMENT")
    if missing:
        return PreflightCheck(
            name="llm:config",
            status="failed",
            message="LLM is enabled but required configuration is missing: " + ", ".join(missing),
            remediation="Populate .env or exported PCA_LLM_* variables, or set PCA_LLM_ENABLED=false for degraded fallback mode.",
            details={"missing": missing},
        )
    return PreflightCheck(
        name="llm:config",
        status="ok",
        message="LLM configuration keys are present.",
        details={"endpoint_present": True, "deployment": config.llm_deployment},
    )


async def _browser_launch_check(timeout_seconds: int = 25) -> PreflightCheck:
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:
        return PreflightCheck(
            name="playwright:chromium_launch",
            status="failed",
            message=f"Playwright import failed: {type(exc).__name__}: {exc}",
            remediation="Install dependencies and run: pdm run playwright install chromium",
        )

    async def _launch() -> None:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            await browser.close()

    try:
        await asyncio.wait_for(_launch(), timeout=timeout_seconds)
        return PreflightCheck(
            name="playwright:chromium_launch",
            status="ok",
            message="Chromium launched successfully.",
        )
    except Exception as exc:
        return PreflightCheck(
            name="playwright:chromium_launch",
            status="failed",
            message=f"Chromium launch failed: {type(exc).__name__}: {exc}",
            remediation="Run: pdm run playwright install chromium. In AzureML, also ensure browser system dependencies are available.",
        )


async def run_runtime_preflight(
    *,
    output_root: Path,
    config: Config | None = None,
    check_browser_launch: bool = False,
    browser_timeout_seconds: int = 25,
) -> RuntimePreflightReport:
    cfg = config or get_config()
    checks: list[PreflightCheck] = [
        _module_check("crawl4ai", remediation="Install core dependencies with pdm install --prod."),
        _module_check("playwright", remediation="Install Playwright and run: pdm run playwright install chromium."),
        _module_check("httpx"),
        _module_check("PIL", remediation="Install Pillow through project dependencies."),
        _output_root_check(Path(output_root)),
        _llm_config_check(cfg),
    ]
    if check_browser_launch:
        checks.append(await _browser_launch_check(timeout_seconds=browser_timeout_seconds))
    failed = [c for c in checks if c.status == "failed"]
    status = "failed" if failed else "ok"
    return RuntimePreflightReport(status=status, checks=checks)


def write_preflight_report(report: RuntimePreflightReport, path: Path | None) -> None:
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


__all__ = ["PreflightCheck", "RuntimePreflightReport", "run_runtime_preflight", "write_preflight_report"]
