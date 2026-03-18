"""Tests for AI security review service."""

import json
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from app.services.security_review import (
    run_security_review,
    _parse_review_response,
    SecurityReviewResult,
)


class TestParseReviewResponse:
    def test_parse_json(self):
        text = json.dumps({
            "danger_score": 2,
            "summary": "Safe command",
            "concerns": ["uses network"],
            "recommendation": "approve",
        })
        result = _parse_review_response(text, {"raw": True})
        assert result.danger_score == 2
        assert result.summary == "Safe command"
        assert result.concerns == ["uses network"]
        assert result.recommendation == "approve"

    def test_parse_with_markdown_fences(self):
        text = '```json\n{"danger_score": 1, "summary": "Pure logic", "concerns": [], "recommendation": "approve"}\n```'
        result = _parse_review_response(text, {})
        assert result.danger_score == 1
        assert result.recommendation == "approve"

    def test_parse_invalid_json(self):
        with pytest.raises(RuntimeError, match="parse"):
            _parse_review_response("not json at all", {})

    def test_defaults_on_missing_fields(self):
        text = json.dumps({})
        result = _parse_review_response(text, {})
        assert result.danger_score == 5  # Default to most dangerous
        assert result.recommendation == "flag_for_review"


class TestRunSecurityReview:
    @pytest.mark.asyncio
    async def test_claude_review(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "content": [{"text": json.dumps({
                "danger_score": 2,
                "summary": "Uses API",
                "concerns": [],
                "recommendation": "approve",
            })}]
        }

        with patch("app.services.security_review.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(return_value=MagicMock(
                post=AsyncMock(return_value=mock_response)
            ))
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await run_security_review("print('hello')", "claude", "sk-test")

        assert result.danger_score == 2
        assert result.recommendation == "approve"

    @pytest.mark.asyncio
    async def test_openai_review(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": json.dumps({
                "danger_score": 1,
                "summary": "Calculator",
                "concerns": [],
                "recommendation": "approve",
            })}}]
        }

        with patch("app.services.security_review.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(return_value=MagicMock(
                post=AsyncMock(return_value=mock_response)
            ))
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await run_security_review("1+1", "openai", "sk-test")

        assert result.danger_score == 1

    @pytest.mark.asyncio
    async def test_invalid_provider(self):
        with pytest.raises(ValueError, match="Unsupported"):
            await run_security_review("code", "gemini", "key")

    @pytest.mark.asyncio
    async def test_claude_api_error(self):
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "Unauthorized"

        with patch("app.services.security_review.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(return_value=MagicMock(
                post=AsyncMock(return_value=mock_response)
            ))
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(RuntimeError, match="Claude API error"):
                await run_security_review("code", "claude", "bad-key")
