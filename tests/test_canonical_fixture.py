from scripts.benchmark_canonical_fixture import evaluate


def test_fixture_metrics_distinguish_admission_from_grouping_errors():
    gold = {"groups": [["a", "b"], ["c"], ["d"]], "excluded": ["n"]}
    result = evaluate(
        [{"candidate_ids": ["a", "c", "n"]}, {"candidate_ids": ["b"]}], gold
    )
    assert result["missed_supported"] == ["d"]
    assert result["unsupported_admissions"] == ["n"]
    assert result["wrong_merge_pairs"] == [["a", "c"]]
    assert result["split_equivalent_pairs"] == [["a", "b"]]
