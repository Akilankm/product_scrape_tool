from product_scraping_agent.prompts import P


def test_product_evidence_prompt_requires_schema_stable_semantic_enrichment():
    prompt = P.PRODUCT_EVIDENCE_JSON.system
    assert "preserving the requested schema shape" in prompt
    assert "Do not add new top-level sections" in prompt
    assert "normalized_value" in prompt
    assert "coding_relevance" in prompt
    assert "transferability" in prompt
    assert "downstream coding" in prompt


def test_claims_prompt_requires_coding_ready_decision_tables():
    prompt = P.CLAIMS_MD.system
    assert "downstream-LLM-readable" in prompt
    assert "normalized value" in prompt
    assert "ready_for_coding" in prompt
    assert "identity confidence" in prompt
    assert "recommended downstream action" in prompt


def test_image_prompt_requires_coding_relevant_visual_facts():
    prompt = P.IMAGE_VISION.system
    assert "product coding" in prompt
    assert "age labels" in prompt
    assert "piece counts" in prompt
    assert "battery/electronic" in prompt
