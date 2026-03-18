"""Tests for store authentication."""

import time
from unittest.mock import patch

import jwt
import pytest
from fastapi import HTTPException

from app.auth import validate_household_jwt


class TestValidateHouseholdJWT:
    def _make_token(self, secret: str, household_id: str, expired: bool = False) -> str:
        payload = {"household_id": household_id}
        if expired:
            payload["exp"] = int(time.time()) - 100
        else:
            payload["exp"] = int(time.time()) + 3600
        return jwt.encode(payload, secret, algorithm="HS256")

    @patch("app.auth.get_settings")
    def test_valid_token(self, mock_settings):
        secret = "test-secret"
        mock_settings.return_value.store_jwt_secret = secret
        token = self._make_token(secret, "household-1")

        result = validate_household_jwt(
            authorization=f"Bearer {token}",
            x_household_id="household-1",
        )
        assert result == "household-1"

    @patch("app.auth.get_settings")
    def test_expired_token(self, mock_settings):
        secret = "test-secret"
        mock_settings.return_value.store_jwt_secret = secret
        token = self._make_token(secret, "household-1", expired=True)

        with pytest.raises(HTTPException) as exc_info:
            validate_household_jwt(
                authorization=f"Bearer {token}",
                x_household_id="household-1",
            )
        assert exc_info.value.status_code == 401

    @patch("app.auth.get_settings")
    def test_household_mismatch(self, mock_settings):
        secret = "test-secret"
        mock_settings.return_value.store_jwt_secret = secret
        token = self._make_token(secret, "household-1")

        with pytest.raises(HTTPException) as exc_info:
            validate_household_jwt(
                authorization=f"Bearer {token}",
                x_household_id="household-2",
            )
        assert exc_info.value.status_code == 403

    @patch("app.auth.get_settings")
    def test_invalid_header(self, mock_settings):
        mock_settings.return_value.store_jwt_secret = "secret"

        with pytest.raises(HTTPException) as exc_info:
            validate_household_jwt(
                authorization="NotBearer token",
                x_household_id="h1",
            )
        assert exc_info.value.status_code == 401

    @patch("app.auth.get_settings")
    def test_no_secret_configured(self, mock_settings):
        mock_settings.return_value.store_jwt_secret = ""

        with pytest.raises(HTTPException) as exc_info:
            validate_household_jwt(
                authorization="Bearer token",
                x_household_id="h1",
            )
        assert exc_info.value.status_code == 503
