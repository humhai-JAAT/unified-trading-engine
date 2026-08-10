"""Verifies GrowwAccount's auth-flow branching in _get_client() — TOTP-based
(preferred) vs approval/secret-based (backward-compat only), added 2026-08-10
alongside the Groww TOTP switch. Flagged as a test-coverage gap in that day's
code review (an auth-critical branch with no test asserting which flow gets
used), since it's exactly the kind of thing that could silently regress back
to secret-only without anyone noticing.
"""

from unittest.mock import MagicMock, patch

from engine.broker_accounts import GrowwAccount


@patch("pyotp.TOTP")
@patch("growwapi.GrowwAPI")
def test_get_client_uses_totp_flow_when_totp_secret_is_set(mock_groww_api, mock_totp):
    mock_totp.return_value.now.return_value = "123456"
    mock_groww_api.get_access_token.return_value = "fake-token"

    account = GrowwAccount(account_id="groww_1", api_key="key", totp_secret="SECRETSEED")
    account._get_client()

    mock_totp.assert_called_once_with("SECRETSEED")
    mock_groww_api.get_access_token.assert_called_once_with("key", totp="123456")
    mock_groww_api.assert_called_once_with("fake-token")


@patch("pyotp.TOTP")
@patch("growwapi.GrowwAPI")
def test_get_client_uses_secret_flow_when_no_totp_secret(mock_groww_api, mock_totp):
    mock_groww_api.get_access_token.return_value = "fake-token"

    account = GrowwAccount(account_id="groww_1", api_key="key", api_secret="approval-secret")
    account._get_client()

    mock_totp.assert_not_called()
    mock_groww_api.get_access_token.assert_called_once_with("key", secret="approval-secret")


@patch("pyotp.TOTP")
@patch("growwapi.GrowwAPI")
def test_get_client_prefers_totp_when_both_are_set(mock_groww_api, mock_totp):
    """If an account somehow has both (e.g. mid-migration), TOTP must win —
    it's the one with no daily-approval requirement."""
    mock_totp.return_value.now.return_value = "123456"
    mock_groww_api.get_access_token.return_value = "fake-token"

    account = GrowwAccount(account_id="groww_1", api_key="key",
                            api_secret="approval-secret", totp_secret="SECRETSEED")
    account._get_client()

    mock_groww_api.get_access_token.assert_called_once_with("key", totp="123456")


def test_is_configured_true_with_either_auth_method():
    assert GrowwAccount(account_id="g", api_key="key", totp_secret="s").is_configured() is True
    assert GrowwAccount(account_id="g", api_key="key", api_secret="s").is_configured() is True
    assert GrowwAccount(account_id="g", api_key="key").is_configured() is False
    assert GrowwAccount(account_id="g", api_key=None, totp_secret="s").is_configured() is False
