from gen_automation.domain.canonical import canonical_sha256


def test_canonical_hash_ignores_mapping_order() -> None:
    assert canonical_sha256({"a": 1, "b": 2}) == canonical_sha256({"b": 2, "a": 1})
