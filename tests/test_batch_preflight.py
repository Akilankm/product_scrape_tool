import csv

import pytest

from product_scraping_agent.batch_preflight import prepare_unique_batch_input_csv


def _rows(path):
    with path.open('r', encoding='utf-8', newline='') as f:
        return list(csv.DictReader(f))


def test_prepare_unique_batch_input_csv_suffixes_duplicate_input_ids(tmp_path):
    input_csv = tmp_path / 'input.csv'
    output_csv = tmp_path / 'output.csv'
    input_csv.write_text(
        'input_id,product_url\n'
        'SKU1,https://example.org/a\n'
        'SKU1,https://example.org/b\n'
        'SKU2,https://example.org/c\n',
        encoding='utf-8',
    )

    result = prepare_unique_batch_input_csv(input_csv, output_csv=output_csv)
    rows = _rows(result.effective_input_csv)

    assert result.changed is True
    assert result.duplicate_groups == 1
    assert result.duplicate_rows == 2
    assert rows[0]['input_id'] == 'SKU1'
    assert rows[1]['input_id'] == 'SKU1__DUP02'
    assert rows[1]['original_input_id'] == 'SKU1'
    assert rows[1]['input_id_collision_resolved'] == 'True'


def test_prepare_unique_batch_input_csv_can_fail_on_duplicates(tmp_path):
    input_csv = tmp_path / 'input.csv'
    output_csv = tmp_path / 'output.csv'
    input_csv.write_text(
        'input_id,product_url\n'
        'SKU1,https://example.org/a\n'
        'SKU1,https://example.org/b\n',
        encoding='utf-8',
    )

    with pytest.raises(ValueError):
        prepare_unique_batch_input_csv(input_csv, output_csv=output_csv, fail_on_duplicate_input_id=True)
