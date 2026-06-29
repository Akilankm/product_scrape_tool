import json

from product_scraping_agent.artifact_audit import audit_artifact_dir, audit_artifact_root, write_audit_outputs


def test_artifact_audit_flags_incomplete_artifact(tmp_path):
    row = tmp_path / 'ROW_001'
    (row / 'retailer' / 'manifests').mkdir(parents=True)
    (row / 'request.json').write_text('{}', encoding='utf-8')

    audit = audit_artifact_dir(row)

    assert audit.artifact_status == 'incomplete_no_terminal_marker'
    assert audit.ready_for_coding is False
    assert 'scrape_result.json' in audit.missing_files
    assert audit.vision_md_empty is True


def test_artifact_audit_ready_artifact(tmp_path):
    row = tmp_path / 'ROW_002'
    retailer = row / 'retailer'
    manifests = retailer / 'manifests'
    images = retailer / 'images'
    manifests.mkdir(parents=True)
    images.mkdir(parents=True)
    (row / '_COMPLETE.json').write_text('{}', encoding='utf-8')
    (row / 'request.json').write_text('{}', encoding='utf-8')
    (row / 'scrape_result.json').write_text(json.dumps({
        'artifact_quality': 'strong',
        'requires_manual_review': False,
        'visual_evidence_status': 'final_product_images_available',
    }), encoding='utf-8')
    for name in ['source.md', 'product_evidence.json', 'quality_report.json', 'source_alignment_report.json', 'vision.md']:
        (retailer / name).write_text('ok', encoding='utf-8')
    for name in ['artifact_manifest.json', 'image_manifest.json', 'agent_trace.json']:
        (manifests / name).write_text('{}', encoding='utf-8')
    (images / 'product_001.png').write_bytes(b'fake')

    audit = audit_artifact_dir(row)

    assert audit.artifact_status == 'ready_for_coding'
    assert audit.ready_for_coding is True
    assert audit.image_file_count == 1


def test_artifact_audit_outputs_summary(tmp_path):
    row = tmp_path / 'ROW_003'
    row.mkdir()
    rows = audit_artifact_root(tmp_path)
    csv_path = tmp_path / 'audit.csv'
    json_path = tmp_path / 'audit.json'

    summary = write_audit_outputs(rows, output_csv=csv_path, output_json=json_path)

    assert summary['total_artifacts'] == 1
    assert summary['not_ready'] == 1
    assert csv_path.exists()
    assert json_path.exists()
