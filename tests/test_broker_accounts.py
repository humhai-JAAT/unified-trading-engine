"""Verifies GrowwAccount's auth-flow branching in _get_client() — TOTP-based
(preferred) vs approval/secret-based (backward-compat only), added 2026-08-10
alongside the Groww TOTP switch. Flagged as a test-coverage gap in that day's
code review (an auth-critical branch with no test asserting which flow gets
used), since it's exactly the kind of thing that could silently regress back
to secret-only without anyone noticing.
"""

from unittest.mock import MagicMock, patch

from engine.broker_accounts import GrowwAccount


def _isolated_account(tmp_path, **kwargs) -> GrowwAccount:
    """Uses a throwaway account_id + redirects the token-cache path into
    tmp_path — 2026-08-11 finding: tests using the real "groww_1"/"groww_2"
    account_ids would read/write the SAME data/groww_token_cache_*.json file
    the actual production account uses, letting a real cached token silently
    short-circuit the mocked get_access_token() call these tests assert on."""
    account = GrowwAccount(account_id="test_isolated", **kwargs)
    account._token_cache_path = tmp_path / "token_cache.json"
    return account


@patch("pyotp.TOTP")
@patch("growwapi.GrowwAPI")
def test_get_client_uses_totp_flow_when_totp_secret_is_set(mock_groww_api, mock_totp, tmp_path):
    mock_totp.return_value.now.return_value = "123456"
    mock_groww_api.get_access_token.return_value = "fake-token"

    account = _isolated_account(tmp_path, api_key="key", totp_secret="SECRETSEED")
    account._get_client()

    mock_totp.assert_called_once_with("SECRETSEED")
    mock_groww_api.get_access_token.assert_called_once_with("key", totp="123456")
    mock_groww_api.assert_called_once_with("fake-token")


@patch("pyotp.TOTP")
@patch("growwapi.GrowwAPI")
def test_get_client_uses_secret_flow_when_no_totp_secret(mock_groww_api, mock_totp, tmp_path):
    mock_groww_api.get_access_token.return_value = "fake-token"

    account = _isolated_account(tmp_path, api_key="key", api_secret="approval-secret")
    account._get_client()

    mock_totp.assert_not_called()
    mock_groww_api.get_access_token.assert_called_once_with("key", secret="approval-secret")


@patch("pyotp.TOTP")
@patch("growwapi.GrowwAPI")
def test_get_client_prefers_totp_when_both_are_set(mock_groww_api, mock_totp, tmp_path):
    """If an account somehow has both (e.g. mid-migration), TOTP must win —
    it's the one with no daily-approval requirement."""
    mock_totp.return_value.now.return_value = "123456"
    mock_groww_api.get_access_token.return_value = "fake-token"

    account = _isolated_account(tmp_path, api_key="key",
                                 api_secret="approval-secret", totp_secret="SECRETSEED")
    account._get_client()

    mock_groww_api.get_access_token.assert_called_once_with("key", totp="123456")


@patch("pyotp.TOTP")
@patch("growwapi.GrowwAPI")
def test_get_client_reuses_a_valid_cached_token_without_calling_get_access_token(
    mock_groww_api, mock_totp, tmp_path
):
    """2026-08-11 live finding: get_access_token() has its own (separate,
    stricter) rate limit from the regular market-data one — repeated
    short-lived process invocations each starting with an empty in-memory
    cache exhausted it and broke the deployed Cloud app's own candle fetches
    too, since they share one account's budget. Persisting to disk lets a
    still-valid token survive a process restart instead of re-authenticating
    every time."""
    import json
    import time

    account = _isolated_account(tmp_path, api_key="key", totp_secret="SECRETSEED")
    account._token_cache_path.write_text(json.dumps({"token": "cached-token", "expires_at": time.time() + 3600}))

    account._get_client()

    mock_groww_api.get_access_token.assert_not_called()
    mock_totp.assert_not_called()
    mock_groww_api.assert_called_once_with("cached-token")


@patch("pyotp.TOTP")
@patch("growwapi.GrowwAPI")
def test_get_client_ignores_an_expired_cached_token(mock_groww_api, mock_totp, tmp_path):
    import json
    import time

    mock_totp.return_value.now.return_value = "123456"
    mock_groww_api.get_access_token.return_value = "fresh-token"

    account = _isolated_account(tmp_path, api_key="key", totp_secret="SECRETSEED")
    account._token_cache_path.write_text(json.dumps({"token": "stale-token", "expires_at": time.time() - 10}))

    account._get_client()

    mock_groww_api.get_access_token.assert_called_once_with("key", totp="123456")
    mock_groww_api.assert_called_once_with("fresh-token")


def test_is_configured_true_with_either_auth_method():
    assert GrowwAccount(account_id="g", api_key="key", totp_secret="s").is_configured() is True
    assert GrowwAccount(account_id="g", api_key="key", api_secret="s").is_configured() is True
    assert GrowwAccount(account_id="g", api_key="key").is_configured() is False
    assert GrowwAccount(account_id="g", api_key=None, totp_secret="s").is_configured() is False
