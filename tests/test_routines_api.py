"""Tests for routine generation API endpoints."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.services.routine_generator import RoutineGenerationResult


SAMPLE_COMMANDS: list[dict] = [
    {
        "name": "get_weather",
        "description": "Get the current weather forecast",
        "parameters": [],
        "keywords": ["weather"],
    },
    {
        "name": "get_news",
        "description": "Get latest news headlines",
        "parameters": [{"name": "category", "type": "string"}],
        "keywords": ["news"],
    },
]


class TestRoutineModelsEndpoint:
    def test_get_models(self, client) -> None:
        resp = client.get("/v1/routines/models")
        assert resp.status_code == 200
        data = resp.json()
        assert "models" in data
        assert len(data["models"]) > 0

        # Each model should have expected fields
        model = data["models"][0]
        assert "id" in model
        assert "display_name" in model
        assert "provider" in model
        assert "estimated_cost" in model
        assert "estimated_cost_usd" in model


class TestRoutineGenerateEndpoint:
    def test_missing_api_key(self, client) -> None:
        resp = client.post("/v1/routines/generate", json={
            "available_commands": SAMPLE_COMMANDS,
            "llm_api_key": "   ",
        })
        assert resp.status_code == 400
        assert "API key" in resp.json()["detail"]

    def test_empty_commands(self, client) -> None:
        resp = client.post("/v1/routines/generate", json={
            "available_commands": [],
            "llm_api_key": "sk-test-key",
        })
        assert resp.status_code == 400
        assert "command" in resp.json()["detail"].lower()

    def test_success_with_mocked_generator(self, client) -> None:
        mock_result = RoutineGenerationResult(
            routines=[
                {
                    "id": "daily_update",
                    "name": "Daily Update",
                    "trigger_phrases": ["daily update", "catch me up", "what's new"],
                    "steps": [
                        {"command": "get_weather", "args": [], "label": "weather"},
                        {"command": "get_news", "args": [{"key": "category", "value": "general"}], "label": "news"},
                    ],
                    "response_instruction": "Summarize everything.",
                    "response_length": "medium",
                    "background": None,
                }
            ],
            explanation="Generated a daily update routine.",
            warnings=["Some warning"],
        )

        with patch(
            "app.services.routine_generator.generate_routines",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_gen:
            resp = client.post("/v1/routines/generate", json={
                "available_commands": SAMPLE_COMMANDS,
                "llm_api_key": "sk-test-key",
                "model": "haiku",
                "user_prompt": "Make morning routines",
            })

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["routines"]) == 1
        assert data["routines"][0]["id"] == "daily_update"
        assert data["explanation"] == "Generated a daily update routine."
        assert data["validation_warnings"] == ["Some warning"]

    def test_llm_error_returns_502(self, client) -> None:
        with patch(
            "app.services.routine_generator.generate_routines",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Claude API error: 500 — Internal Server Error"),
        ):
            resp = client.post("/v1/routines/generate", json={
                "available_commands": SAMPLE_COMMANDS,
                "llm_api_key": "sk-test-key",
            })

        assert resp.status_code == 502
        assert "Claude API error" in resp.json()["detail"]
