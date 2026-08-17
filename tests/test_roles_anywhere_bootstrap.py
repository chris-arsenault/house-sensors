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


def test_bootstrap_fetches_secrets_and_refuses_to_start_without_them() -> None:
    """The container reads its own secrets, and a failure stops it.

    A service that started anyway would run on whatever was already in its
    environment, which is the stale deploy-delivered value this replaces.
    """
    script = (
        Path(__file__).parents[1]
        / "jobs"
        / "downsampling"
        / "truenas-roles-anywhere-bootstrap"
    )
    source = script.read_text()

    assert 'secret_prefix="${ENV_PREFIX}_SECRET_"' in source
    assert "aws ssm get-parameter" in source
    assert "--with-decryption" in source
    # Every failure path exits rather than falling through to exec.
    assert 'echo "${path_var} is empty" >&2\n      exit 1' in source
    assert 'echo "${path} resolved to nothing" >&2\n      exit 1' in source
