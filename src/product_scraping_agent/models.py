"""Pydantic DTOs for URL-in / artifact-out product scraping."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .text_utils import digits_only

_DTO_CONFIG = ConfigDict(arbitrary_types_allowed=True, extra="forbid")


class ProductInputContext(BaseModel):
    """Optional product identity hints supplied alongside the URL."""

    model_config = _DTO_CONFIG

    main_text: str = ""
    ean: str = ""
    retailer_name: str = ""
    country_code: str = ""

    @field_validator("ean")
    @classmethod
    def normalize_ean(cls, value: str) -> str:
        return digits_only(value)

    @field_validator("country_code")
    @classmethod
    def normalize_country(cls, value: str) -> str:
        return (value or "").strip().upper()

    def has_any(self) -> bool:
        return any([self.main_text, self.ean, self.retailer_name, self.country_code])

    def as_prompt_block(self) -> str:
        rows = [
            ("main_text", self.main_text),
            ("ean", self.ean),
            ("retailer_name", self.retailer_name),
            ("country_code", self.country_code),
        ]
        body = "\n".join(f"- {k}: {v or '(not provided)'}" for k, v in rows)
        return body

    def compact_hint(self, fallback: str = "") -> str:
        parts: list[str] = []
        if self.main_text:
            parts.append(self.main_text)
        if self.ean:
            parts.append(f"EAN {self.ean}")
        if self.retailer_name:
            parts.append(f"retailer {self.retailer_name}")
        if self.country_code:
            parts.append(f"country {self.country_code}")
        return " | ".join(parts) or fallback


class ScrapeRequest(BaseModel):
    """Public input contract for the isolated product scraping agent.

    `product_url` is the only required field. The remaining fields are optional
    identity hints used for artifact provenance, image relevance checks, and
    claims synthesis grounding. They do not trigger search.
    """

    model_config = _DTO_CONFIG

    product_url: str
    scrape_id: str | None = None

    # Optional user/business identifiers supplied with the product URL.
    main_text: str = ""
    ean: str = ""
    retailer_name: str = ""
    country_code: str = ""

    # Optional override for image/claims prompts. If omitted, it is derived from
    # main_text/EAN/retailer/country where available.
    product_hint: str = ""

    output_root: Path | None = None
    retailer_label: str = "retailer"
    max_images: int = 30
    vision_max: int = 12
    max_agent_iterations: int = 2
    strict_product_only: bool = True
    write_raw_debug: bool | None = None

    @field_validator("ean")
    @classmethod
    def normalize_ean(cls, value: str) -> str:
        return digits_only(value)

    @field_validator("country_code")
    @classmethod
    def normalize_country(cls, value: str) -> str:
        return (value or "").strip().upper()

    @model_validator(mode="after")
    def require_url(self) -> "ScrapeRequest":
        if not self.product_url or not self.product_url.strip():
            raise ValueError("product_url is required")
        return self

    @property
    def input_context(self) -> ProductInputContext:
        return ProductInputContext(
            main_text=self.main_text.strip(),
            ean=self.ean.strip(),
            retailer_name=self.retailer_name.strip(),
            country_code=self.country_code.strip(),
        )

    def resolved_product_hint(self) -> str:
        return (self.product_hint or "").strip() or self.input_context.compact_hint()


class ImageRef(BaseModel):
    """One product image: original URL, local path, hashes, and vision note."""

    model_config = _DTO_CONFIG

    url: str
    local_path: Path | None = None
    width: int = 0
    height: int = 0
    bytes_size: int = 0
    sha8: str = ""
    phash: str = ""
    mime: str = ""
    alt: str = ""
    description: str = ""
    relevance: str = ""
    error: str = ""


class TableRef(BaseModel):
    """One HTML table extracted from the rendered product page."""

    model_config = _DTO_CONFIG

    index: int
    caption: str = ""
    rows: int = 0
    cols: int = 0
    local_path: Path | None = None
    markdown: str = ""


class ScrapeResult(BaseModel):
    """Machine-readable public result contract."""

    model_config = _DTO_CONFIG

    success: bool = False
    scrape_id: str = ""
    product_url: str = ""
    final_url: str = ""
    title: str = ""
    output_dir: Path | None = None

    input_context: ProductInputContext = Field(default_factory=ProductInputContext)

    request_json_path: Path | None = None
    scrape_result_json_path: Path | None = None
    source_md_path: Path | None = None
    claims_md_path: Path | None = None
    vision_md_path: Path | None = None
    metadata_json_path: Path | None = None
    image_manifest_path: Path | None = None
    table_manifest_path: Path | None = None
    artifact_manifest_path: Path | None = None
    product_evidence_md_path: Path | None = None
    product_evidence_json_path: Path | None = None
    noise_report_json_path: Path | None = None
    agent_trace_json_path: Path | None = None
    raw_debug_dir: Path | None = None

    image_count: int = 0
    image_downloaded_count: int = 0
    vision_described_count: int = 0
    table_count: int = 0
    json_ld_count: int = 0
    agent_iterations: int = 0
    elapsed_seconds: float = 0.0
    error: str = ""


class PlannedScrapeAction(BaseModel):
    """One same-page follow-up extraction action proposed by the LLM planner."""

    model_config = _DTO_CONFIG

    action: Literal["stop", "full_page_scroll", "expand_common_sections", "extract_gallery_sources", "retry_relaxed"]
    reason: str = ""
    priority: int = 1


class AgentPlan(BaseModel):
    """LLM planner output for iterative same-URL scraping."""

    model_config = _DTO_CONFIG

    enough_evidence: bool = False
    missing_evidence: list[str] = Field(default_factory=list)
    actions: list[PlannedScrapeAction] = Field(default_factory=list)
    stop_reason: str = ""


class ProductEvidence(BaseModel):
    """Normalized product-only evidence object used as the main downstream artifact."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="allow")

    product_focus_summary: str = ""
    product_identity: dict[str, Any] = Field(default_factory=dict)
    retailer_claims: list[dict[str, Any]] = Field(default_factory=list)
    product_only_text_blocks: list[dict[str, Any]] = Field(default_factory=list)
    structured_claims: list[dict[str, Any]] = Field(default_factory=list)
    table_claims: list[dict[str, Any]] = Field(default_factory=list)
    visual_claims: list[dict[str, Any]] = Field(default_factory=list)
    discrepancies: list[dict[str, Any]] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    noise_exclusion_summary: dict[str, Any] = Field(default_factory=dict)
    quality: dict[str, Any] = Field(default_factory=dict)


class ScrapedProduct(BaseModel):
    """Rich internal scrape result."""

    model_config = _DTO_CONFIG

    scrape_id: str = ""
    url: str
    final_url: str = ""
    title: str = ""
    output_dir: Path | None = None
    input_context: ProductInputContext = Field(default_factory=ProductInputContext)

    raw_markdown: str = ""
    raw_html: str = ""
    json_ld: list[dict[str, Any]] = Field(default_factory=list)
    claims_markdown: str = ""
    images: list[ImageRef] = Field(default_factory=list)
    tables: list[TableRef] = Field(default_factory=list)

    request_json_path: Path | None = None
    scrape_result_json_path: Path | None = None
    source_md_path: Path | None = None
    claims_md_path: Path | None = None
    vision_md_path: Path | None = None
    metadata_json_path: Path | None = None
    image_manifest_path: Path | None = None
    table_manifest_path: Path | None = None
    artifact_manifest_path: Path | None = None
    product_evidence_md_path: Path | None = None
    product_evidence_json_path: Path | None = None
    noise_report_json_path: Path | None = None
    agent_trace_json_path: Path | None = None
    raw_debug_dir: Path | None = None

    product_evidence: dict[str, Any] = Field(default_factory=dict)
    agent_trace: list[dict[str, Any]] = Field(default_factory=list)
    agent_iterations: int = 0

    success: bool = False
    error: str = ""
    elapsed_seconds: float = 0.0

    def to_scrape_result(self) -> ScrapeResult:
        return ScrapeResult(
            success=self.success,
            scrape_id=self.scrape_id,
            product_url=self.url,
            final_url=self.final_url,
            title=self.title,
            output_dir=self.output_dir,
            input_context=self.input_context,
            request_json_path=self.request_json_path,
            scrape_result_json_path=self.scrape_result_json_path,
            source_md_path=self.source_md_path,
            claims_md_path=self.claims_md_path,
            vision_md_path=self.vision_md_path,
            metadata_json_path=self.metadata_json_path,
            image_manifest_path=self.image_manifest_path,
            table_manifest_path=self.table_manifest_path,
            artifact_manifest_path=self.artifact_manifest_path,
            product_evidence_md_path=self.product_evidence_md_path,
            product_evidence_json_path=self.product_evidence_json_path,
            noise_report_json_path=self.noise_report_json_path,
            agent_trace_json_path=self.agent_trace_json_path,
            raw_debug_dir=self.raw_debug_dir,
            image_count=len(self.images),
            image_downloaded_count=sum(1 for img in self.images if img.local_path),
            vision_described_count=sum(1 for img in self.images if img.description),
            table_count=len(self.tables),
            json_ld_count=len(self.json_ld),
            agent_iterations=self.agent_iterations,
            elapsed_seconds=self.elapsed_seconds,
            error=self.error,
        )


__all__ = [
    "ProductInputContext",
    "ScrapeRequest",
    "ScrapeResult",
    "ImageRef",
    "TableRef",
    "PlannedScrapeAction",
    "AgentPlan",
    "ProductEvidence",
    "ScrapedProduct",
]
