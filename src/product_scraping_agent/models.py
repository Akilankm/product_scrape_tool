"""Pydantic DTOs for URL-in / artifact-out product scraping."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .text_utils import digits_only

_DTO_CONFIG = ConfigDict(arbitrary_types_allowed=True, extra="forbid")


class ProductInputContext(BaseModel):
    """Optional product identity hints supplied alongside the URL.

    `retailer_name` and `country_code` represent the requested/target market
    context for backward compatibility. Actual scraped source context is stored
    separately in SourceAlignmentContext.
    """

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
            ("requested_retailer_name", self.retailer_name),
            ("requested_country_code", self.country_code),
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
            parts.append(f"requested retailer {self.retailer_name}")
        if self.country_code:
            parts.append(f"requested country {self.country_code}")
        return " | ".join(parts) or fallback


class EvidenceSourceItem(BaseModel):
    """Optional upstream evidence already produced by the search/discovery layer.

    The scraping agent does not perform search. These items are only consumed
    when the caller passes them in, for example SerpAPI snippets, AI Mode
    answers, cached/indexed snippets, or manually supplied discovery evidence.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="allow")

    source_type: str = "upstream"
    title: str = ""
    url: str = ""
    text: str = ""
    evidence_id: str = ""
    confidence: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)


class UpstreamEvidenceBundle(BaseModel):
    """Search/discovery evidence handed to the scraper by the caller.

    This is not a search feature. It is an evidence recovery input. It lets the
    scraper build a grounded product artifact when browser rendering is blocked
    or weak, while still tagging those claims as upstream/indexed evidence.
    """

    model_config = _DTO_CONFIG

    ai_mode_evidence: str = ""
    candidate_snippets: list[str] = Field(default_factory=list)
    search_evidence: list[EvidenceSourceItem] = Field(default_factory=list)
    notes: str = ""

    def has_any(self) -> bool:
        return bool(
            (self.ai_mode_evidence or "").strip()
            or any((x or "").strip() for x in self.candidate_snippets)
            or any((item.text or item.title or item.url or item.raw) for item in self.search_evidence)
            or (self.notes or "").strip()
        )

    def compact(self, *, max_chars: int = 60_000) -> dict[str, Any]:
        data = self.model_dump()
        # Keep the payload bounded before sending to the LLM.
        if len(data.get("ai_mode_evidence") or "") > max_chars:
            data["ai_mode_evidence"] = data["ai_mode_evidence"][:max_chars] + "... [truncated]"
        data["candidate_snippets"] = [
            s[:8_000] + ("... [truncated]" if len(s) > 8_000 else "")
            for s in data.get("candidate_snippets", [])[:20]
        ]
        data["search_evidence"] = data.get("search_evidence", [])[:50]
        return data




class SourceAlignmentContext(BaseModel):
    """Separates the requested market from the actual URL evidence source.

    This prevents fallback URLs from contaminating requested-retailer claims.
    Example pattern handled generically: requested retailer/country may be X,
    while the provided product_url is scraped from Y. No retailer-specific logic
    is hardcoded; the artifact records the alignment and scopes claims safely.
    """

    model_config = _DTO_CONFIG

    product_url: str = ""

    # Original target/market context from the caller/business row.
    requested_retailer_name: str = ""
    requested_country_code: str = ""

    # Actual source URL context. Caller may supply it when known; otherwise the
    # agent records it as unknown instead of guessing.
    source_retailer_name: str = ""
    source_country_code: str = ""
    source_url_role: str = "unknown"

    @field_validator("requested_country_code", "source_country_code")
    @classmethod
    def normalize_country(cls, value: str) -> str:
        return (value or "").strip().upper()

    @field_validator("source_url_role")
    @classmethod
    def normalize_role(cls, value: str) -> str:
        role = (value or "unknown").strip().lower().replace("-", "_").replace(" ", "_")
        return role or "unknown"

    @staticmethod
    def _norm_name(value: str) -> str:
        return "".join(ch for ch in (value or "").lower() if ch.isalnum())

    @property
    def retailer_match(self) -> bool | None:
        requested = self._norm_name(self.requested_retailer_name)
        source = self._norm_name(self.source_retailer_name)
        if not requested or not source:
            return None
        return requested == source

    @property
    def country_match(self) -> bool | None:
        if not self.requested_country_code or not self.source_country_code:
            return None
        return self.requested_country_code == self.source_country_code

    @property
    def alignment_status(self) -> str:
        role = self.source_url_role or "unknown"
        fallback_roles = {
            "fallback", "fallback_alternate_retailer", "marketplace_fallback", "global_fallback",
            "alternate_retailer_same_country", "alternate_retailer_different_country",
            "same_retailer_different_country", "different_retailer", "alternate_source",
        }
        primary_roles = {"primary", "primary_requested_retailer", "requested_retailer", "same_retailer_same_country"}
        if role in primary_roles:
            return "primary_requested_source"
        if role in fallback_roles:
            return "fallback_source_used"
        if self.retailer_match is True and self.country_match is True:
            return "primary_requested_source"
        if self.retailer_match is False or self.country_match is False:
            return "source_context_mismatch"
        return "not_declared"

    @property
    def product_facts_transfer_allowed(self) -> bool:
        # Product identity/fact claims may be used if evidence-grounded, even from
        # a fallback URL, but must remain tagged by source axis.
        return True

    @property
    def requested_retailer_claims_allowed(self) -> bool:
        # Price/availability/delivery/seller claims must not be transferred from
        # a fallback source to the requested retailer/country.
        return self.alignment_status == "primary_requested_source"

    @property
    def source_specific_claim_scope(self) -> str:
        return "requested_retailer_and_country" if self.requested_retailer_claims_allowed else "scraped_source_only"

    def model_report(self) -> dict[str, Any]:
        return {
            "requested_context": {
                "retailer_name": self.requested_retailer_name,
                "country_code": self.requested_country_code,
            },
            "scraped_source": {
                "product_url": self.product_url,
                "retailer_name": self.source_retailer_name,
                "country_code": self.source_country_code,
                "source_url_role": self.source_url_role,
            },
            "alignment_status": self.alignment_status,
            "retailer_match": self.retailer_match,
            "country_match": self.country_match,
            "product_facts_transfer_allowed": self.product_facts_transfer_allowed,
            "requested_retailer_claims_allowed": self.requested_retailer_claims_allowed,
            "source_specific_claim_scope": self.source_specific_claim_scope,
            "policy": (
                "Product-level facts can be used from the scraped source when evidence-grounded. "
                "Retailer-specific claims such as price, availability, delivery, seller, and marketplace terms "
                "are scoped only to the scraped source unless the source matches the requested retailer/country."
            ),
        }

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
    retailer_name: str = ""  # backward-compatible alias for requested_retailer_name
    country_code: str = ""    # backward-compatible alias for requested_country_code

    # Explicit requested vs actual-source context. These are optional and generic.
    requested_retailer_name: str = ""
    requested_country_code: str = ""
    source_retailer_name: str = ""
    source_country_code: str = ""
    source_url_role: str = "unknown"

    # Optional override for image/claims prompts. If omitted, it is derived from
    # main_text/EAN/retailer/country where available.
    product_hint: str = ""

    # Optional evidence from the already-completed search/discovery layer.
    # The scraper never performs search; it only consumes these as grounded
    # recovery evidence when browser access is weak/blocked.
    upstream_ai_evidence: str = ""
    candidate_snippets: list[str] = Field(default_factory=list)
    search_evidence: list[EvidenceSourceItem] = Field(default_factory=list)
    upstream_evidence_notes: str = ""

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

    @field_validator("country_code", "requested_country_code", "source_country_code")
    @classmethod
    def normalize_country(cls, value: str) -> str:
        return (value or "").strip().upper()

    @field_validator("source_url_role")
    @classmethod
    def normalize_source_url_role(cls, value: str) -> str:
        role = (value or "unknown").strip().lower().replace("-", "_").replace(" ", "_")
        return role or "unknown"

    @model_validator(mode="after")
    def require_url(self) -> "ScrapeRequest":
        if not self.product_url or not self.product_url.strip():
            raise ValueError("product_url is required")
        return self

    @property
    def input_context(self) -> ProductInputContext:
        requested_retailer = (self.requested_retailer_name or self.retailer_name or "").strip()
        requested_country = (self.requested_country_code or self.country_code or "").strip().upper()
        return ProductInputContext(
            main_text=self.main_text.strip(),
            ean=self.ean.strip(),
            retailer_name=requested_retailer,
            country_code=requested_country,
        )

    @property
    def source_alignment(self) -> SourceAlignmentContext:
        requested_retailer = (self.requested_retailer_name or self.retailer_name or "").strip()
        requested_country = (self.requested_country_code or self.country_code or "").strip().upper()
        return SourceAlignmentContext(
            product_url=self.product_url.strip(),
            requested_retailer_name=requested_retailer,
            requested_country_code=requested_country,
            source_retailer_name=(self.source_retailer_name or "").strip(),
            source_country_code=(self.source_country_code or "").strip().upper(),
            source_url_role=(self.source_url_role or "unknown").strip(),
        )

    @property
    def upstream_evidence(self) -> UpstreamEvidenceBundle:
        return UpstreamEvidenceBundle(
            ai_mode_evidence=(self.upstream_ai_evidence or "").strip(),
            candidate_snippets=[s.strip() for s in self.candidate_snippets if (s or "").strip()],
            search_evidence=self.search_evidence,
            notes=(self.upstream_evidence_notes or "").strip(),
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
    download_source: str = ""
    download_attempts: list[dict[str, Any]] = Field(default_factory=list)


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

    access_status: str = "unknown"
    access_issue_type: str = ""
    access_issue_reason: str = ""
    geo_restricted: bool = False
    proxy_used: bool = False
    proxy_source: str = ""
    access_attempts: list[dict[str, Any]] = Field(default_factory=list)

    browser_visible: bool = False
    product_details_recovered: bool = False
    recovery_status: str = "not_evaluated"
    evidence_axes_used: list[str] = Field(default_factory=list)

    input_context: ProductInputContext = Field(default_factory=ProductInputContext)
    upstream_evidence: UpstreamEvidenceBundle = Field(default_factory=UpstreamEvidenceBundle)
    source_alignment: SourceAlignmentContext = Field(default_factory=SourceAlignmentContext)

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
    evidence_recovery_report_json_path: Path | None = None
    quality_report_json_path: Path | None = None
    source_alignment_report_json_path: Path | None = None
    agent_trace_json_path: Path | None = None
    raw_debug_dir: Path | None = None

    # Image counters are intentionally separated:
    # - image_candidate_count = URLs/candidates discovered during extraction
    # - image_count / final_image_count = final clean files retained under retailer/images/
    #   after vision confirmation. This avoids interpreting weak/blocked pages as
    #   having real product images when only candidates were seen.
    image_candidate_count: int = 0
    image_count: int = 0
    final_image_count: int = 0
    image_downloaded_count: int = 0
    vision_described_count: int = 0
    table_count: int = 0
    json_ld_count: int = 0
    agent_iterations: int = 0

    # Deterministic quality gate summary copied from retailer/quality_report.json.
    artifact_quality: str = "not_evaluated"
    quality_score: int = 0
    requires_manual_review: bool = False
    missing_critical_fields: list[str] = Field(default_factory=list)
    quality_warnings: list[str] = Field(default_factory=list)

    elapsed_seconds: float = 0.0
    error: str = ""

    @property
    def quality_report_path(self) -> Path | None:
        """Backward-compatible notebook alias for quality_report_json_path."""
        return self.quality_report_json_path

    @property
    def source_alignment_report_path(self) -> Path | None:
        """Backward-compatible notebook alias for source_alignment_report_json_path."""
        return self.source_alignment_report_json_path


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
    source_alignment: dict[str, Any] = Field(default_factory=dict)
    product_identity: dict[str, Any] = Field(default_factory=dict)
    retailer_claims: list[dict[str, Any]] = Field(default_factory=list)
    source_specific_claims: list[dict[str, Any]] = Field(default_factory=list)
    product_only_text_blocks: list[dict[str, Any]] = Field(default_factory=list)
    structured_claims: list[dict[str, Any]] = Field(default_factory=list)
    table_claims: list[dict[str, Any]] = Field(default_factory=list)
    visual_claims: list[dict[str, Any]] = Field(default_factory=list)
    upstream_indexed_claims: list[dict[str, Any]] = Field(default_factory=list)
    url_derived_claims: list[dict[str, Any]] = Field(default_factory=list)
    input_context_claims: list[dict[str, Any]] = Field(default_factory=list)
    discrepancies: list[Any] = Field(default_factory=list)
    gaps: list[Any] = Field(default_factory=list)
    noise_exclusion_summary: dict[str, Any] = Field(default_factory=dict)
    quality: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "retailer_claims",
        "source_specific_claims",
        "product_only_text_blocks",
        "structured_claims",
        "table_claims",
        "visual_claims",
        "upstream_indexed_claims",
        "url_derived_claims",
        "input_context_claims",
        mode="before",
    )
    @classmethod
    def _coerce_list_of_dicts(cls, value: Any) -> list[dict[str, Any]]:
        """LLMs sometimes return a scalar/string where a list of objects is expected.

        Keep the artifact generation robust by preserving the payload as a
        generic claim object rather than failing validation and falling back.
        """
        if value in (None, ""):
            return []
        if isinstance(value, dict):
            return [value]
        if not isinstance(value, list):
            return [{"value": value}]
        out: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, dict):
                out.append(item)
            else:
                out.append({"value": item})
        return out

    @field_validator("gaps", "discrepancies", mode="before")
    @classmethod
    def _coerce_freeform_list(cls, value: Any) -> list[Any]:
        if value in (None, ""):
            return []
        if isinstance(value, list):
            return value
        return [value]


class ScrapedProduct(BaseModel):
    """Rich internal scrape result."""

    model_config = _DTO_CONFIG

    scrape_id: str = ""
    url: str
    final_url: str = ""
    title: str = ""
    output_dir: Path | None = None

    access_status: str = "unknown"
    access_issue_type: str = ""
    access_issue_reason: str = ""
    geo_restricted: bool = False
    proxy_used: bool = False
    proxy_source: str = ""
    access_attempts: list[dict[str, Any]] = Field(default_factory=list)

    browser_visible: bool = False
    product_details_recovered: bool = False
    recovery_status: str = "not_evaluated"
    evidence_axes_used: list[str] = Field(default_factory=list)

    input_context: ProductInputContext = Field(default_factory=ProductInputContext)
    upstream_evidence: UpstreamEvidenceBundle = Field(default_factory=UpstreamEvidenceBundle)
    source_alignment: SourceAlignmentContext = Field(default_factory=SourceAlignmentContext)

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
    evidence_recovery_report_json_path: Path | None = None
    quality_report_json_path: Path | None = None
    source_alignment_report_json_path: Path | None = None
    agent_trace_json_path: Path | None = None
    raw_debug_dir: Path | None = None

    product_evidence: dict[str, Any] = Field(default_factory=dict)
    agent_trace: list[dict[str, Any]] = Field(default_factory=list)
    agent_iterations: int = 0

    success: bool = False
    error: str = ""
    elapsed_seconds: float = 0.0

    def to_scrape_result(self) -> ScrapeResult:
        quality_gate = (self.product_evidence or {}).get("quality_gate") or {}
        return ScrapeResult(
            success=self.success,
            scrape_id=self.scrape_id,
            product_url=self.url,
            final_url=self.final_url,
            title=self.title,
            output_dir=self.output_dir,
            access_status=self.access_status,
            access_issue_type=self.access_issue_type,
            access_issue_reason=self.access_issue_reason,
            geo_restricted=self.geo_restricted,
            proxy_used=self.proxy_used,
            proxy_source=self.proxy_source,
            access_attempts=self.access_attempts,
            browser_visible=self.browser_visible,
            product_details_recovered=self.product_details_recovered,
            recovery_status=self.recovery_status,
            evidence_axes_used=self.evidence_axes_used,
            input_context=self.input_context,
            upstream_evidence=self.upstream_evidence,
            source_alignment=self.source_alignment,
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
            evidence_recovery_report_json_path=self.evidence_recovery_report_json_path,
            quality_report_json_path=self.quality_report_json_path,
            source_alignment_report_json_path=self.source_alignment_report_json_path,
            agent_trace_json_path=self.agent_trace_json_path,
            raw_debug_dir=self.raw_debug_dir,
            image_candidate_count=len(self.images),
            image_count=sum(1 for img in self.images if img.local_path and img.description and str(img.relevance).lower() == "yes"),
            final_image_count=sum(1 for img in self.images if img.local_path and img.description and str(img.relevance).lower() == "yes"),
            image_downloaded_count=sum(1 for img in self.images if img.local_path),
            vision_described_count=sum(1 for img in self.images if img.local_path and img.description),
            table_count=len(self.tables),
            json_ld_count=len(self.json_ld),
            agent_iterations=self.agent_iterations,
            artifact_quality=str(quality_gate.get("artifact_quality") or "not_evaluated"),
            quality_score=int(quality_gate.get("quality_score") or 0),
            requires_manual_review=bool(quality_gate.get("requires_manual_review") or False),
            missing_critical_fields=list(quality_gate.get("missing_critical_fields") or []),
            quality_warnings=list(quality_gate.get("warnings") or []),
            elapsed_seconds=self.elapsed_seconds,
            error=self.error,
        )


__all__ = [
    "ProductInputContext",
    "EvidenceSourceItem",
    "UpstreamEvidenceBundle",
    "SourceAlignmentContext",
    "ScrapeRequest",
    "ScrapeResult",
    "ImageRef",
    "TableRef",
    "PlannedScrapeAction",
    "AgentPlan",
    "ProductEvidence",
    "ScrapedProduct",
]
