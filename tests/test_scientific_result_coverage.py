from miner.scientific_result_v1.coverage import evidence_issues, placeholder_issues, source_blocks


def test_plan_evidence_must_exist_in_the_supplied_source():
    result = {'result_plan': [{'id': 'P.E001.R001', 'source_ids': ['S1'],
              'evidence': [{'source_id': 'S1', 'block_id': 'B0001'}]}], 'unrecovered_regions': []}
    documents = [{'source_id': 'S1', 'text': 'Mean was  4.\nOther text.'}]
    assert not evidence_issues(result, documents, 'P.E001')
    result['result_plan'][0]['evidence'][0]['block_id'] = 'B9999'
    assert evidence_issues(result, documents, 'P.E001')


def test_digitized_placeholders_rejected_without_rejecting_missingness_or_real_coordinates():
    def issues(value, origin):
        return placeholder_issues({'results': [{'fields': [{'id': 'F1', 'value': value, 'origin': origin}]}]})
    assert issues('digitized curve', 'digitized')
    assert issues('see figure', 'source_reported')
    assert issues(None, 'digitized')
    assert not issues([[0, .03], [2, .07]], 'digitized')
    assert not issues('not estimable', 'source_reported')
    assert not issues(None, 'source_reported')
    assert not issues('No numerical group scores reported', 'source_reported')


def test_source_blocks_copy_original_math_and_include_ocr_and_table_cells():
    documents = [{'source_id': 'S1', 'text': 'Condition b/c &gt; k.\n\nEquation α=β.',
                  'page_ocr_text': 'Printed weight 29.0', 'tables': [{'csv_rows': [['0.73', '(0.39–1.36)']]}]}]
    blocks = source_blocks(documents)
    assert len({(b['source_id'], b['block_id']) for b in blocks}) == len(blocks)
    assert [b['text'] for b in blocks] == ['Condition b/c &gt; k.', 'Equation α=β.', 'Printed weight 29.0', '0.73 | (0.39–1.36)']
