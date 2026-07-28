from uuid import RFC_4122

from gen_automation.domain.ids import uuid7


def test_uuid7_has_expected_version_and_variant() -> None:
    generated = uuid7()

    assert generated.version == 7
    assert generated.variant == RFC_4122
