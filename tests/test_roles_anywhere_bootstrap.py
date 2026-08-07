from pathlib import Path


def test_bootstrap_uses_current_unattended_enrollment_contract() -> None:
    script = (
        Path(__file__).parents[1]
        / "jobs"
        / "downsampling"
        / "truenas-roles-anywhere-bootstrap"
    )
    source = script.read_text()

    assert "^spiffe://ahara/[a-z0-9]" in source
    assert "ENROLLMENT_TOKEN" not in source
    assert 'ENDPOINT="${ENROLLMENT_URL}/renew"' in source
    assert 'ENDPOINT="${ENROLLMENT_URL}/enroll"' in source
    assert 'while sleep 86400' in source
