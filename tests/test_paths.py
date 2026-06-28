from pathlib import Path

from product_scraping_agent.pipeline import make_scrape_id, output_dir_for, slug_from_url


def test_make_scrape_id_prefix():
    assert make_scrape_id("x").startswith("x_")


def test_slug_from_url():
    slug = slug_from_url("https://example.com/a/b product?x=1")
    assert slug.startswith("example.com__")
    assert " " not in slug


def test_output_dir_for(tmp_path):
    out = output_dir_for("sid", "https://example.com/p", output_root=tmp_path, retailer_label="retailer")
    assert out == tmp_path / "sid" / "retailer"
    assert out.exists()
