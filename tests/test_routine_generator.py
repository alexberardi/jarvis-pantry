"""Tests for AI routine generator service."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.routine_generator import (
    RoutineGenerationResult,
    _build_system_prompt,
    _parse_response,
    _slugify,
    _validate_routines,
    generate_routines,
)


# ── Sample command metadata ───────────────────────────────────────────

SAMPLE_COMMANDS: list[dict] = [
    {
        "name": "get_weather",
        "description": "Get the current weather forecast",
        "parameters": [
            {"name": "resolved_datetimes", "type": "string", "required": False, "description": "Date for forecast"},
            {"name": "location", "type": "string", "required": False, "description": "Location name"},
        ],
        "keywords": ["weather", "forecast", "temperature"],
        "examples": ["what's the weather", "forecast for tomorrow"],
    },
    {
        "name": "get_calendar_events",
        "description": "Get upcoming calendar events",
        "parameters": [
            {"name": "resolved_datetimes", "type": "string", "required": False, "description": "Date to query"},
        ],
        "keywords": ["calendar", "schedule", "events"],
        "examples": ["what's on my calendar"],
    },
    {
        "name": "get_news",
        "description": "Get latest news headlines",
        "parameters": [
            {"name": "category", "type": "string", "required": False, "description": "News category", "enum_values": ["general", "sports", "tech"]},
            {"name": "count", "type": "string", "required": False, "description": "Number of articles"},
        ],
        "keywords": ["news", "headlines"],
        "examples": ["give me the news"],
    },
]


# ── System prompt tests ───────────────────────────────────────────────

class TestBuildSystemPrompt:
    def test_includes_command_metadata(self) -> None:
        prompt = _build_system_prompt(SAMPLE_COMMANDS)
        assert "get_weather" in prompt
        assert "get_calendar_events" in prompt
        assert "get_news" in prompt
        assert "Get the current weather forecast" in prompt

    def test_includes_parameters(self) -> None:
        prompt = _build_system_prompt(SAMPLE_COMMANDS)
        assert "resolved_datetimes" in prompt
        assert "location" in prompt
        assert "category" in prompt

    def test_includes_reference_routines(self) -> None:
        prompt = _build_system_prompt(SAMPLE_COMMANDS)
        assert "good_morning" in prompt
        assert "morning_briefing" in prompt
        assert "good_night" in prompt

    def test_includes_data_model_spec(self) -> None:
        prompt = _build_system_prompt(SAMPLE_COMMANDS)
        assert "trigger_phrases" in prompt
        assert "response_length" in prompt
        assert "response_instruction" in prompt

    def test_empty_commands(self) -> None:
        prompt = _build_system_prompt([])
        assert "No commands available" in prompt


# ── Response parsing tests ────────────────────────────────────────────

class TestParseResponse:
    def test_parse_valid_json(self) -> None:
        text = json.dumps({
            "routines": [{"id": "test", "name": "Test"}],
            "explanation": "Generated one routine",
        })
        result = _parse_response(text)
        assert len(result["routines"]) == 1
        assert result["explanation"] == "Generated one routine"

    def test_parse_with_markdown_fences(self) -> None:
        text = '```json\n{"routines": [], "explanation": "Empty"}\n```'
        result = _parse_response(text)
        assert result["routines"] == []

    def test_parse_invalid_json(self) -> None:
        with pytest.raises(RuntimeError, match="parse"):
            _parse_response("not json at all")


# ── Slugify tests ─────────────────────────────────────────────────────

class TestSlugify:
    def test_simple_name(self) -> None:
        assert _slugify("Good Morning") == "good_morning"

    def test_special_chars(self) -> None:
        assert _slugify("What's Up?") == "what_s_up"

    def test_already_slug(self) -> None:
        assert _slugify("morning_briefing") == "morning_briefing"


# ── Validation tests ──────────────────────────────────────────────────

class TestValidateRoutines:
    def test_accept_valid_routines(self) -> None:
        routines = [
            {
                "id": "morning_info",
                "name": "Morning Info",
                "trigger_phrases": ["morning info", "give me info", "what's up"],
                "steps": [
                    {"command": "get_weather", "args": [{"key": "resolved_datetimes", "value": "today"}], "label": "weather"},
                    {"command": "get_news", "args": [{"key": "category", "value": "general"}], "label": "news"},
                ],
                "response_instruction": "Give a morning briefing.",
                "response_length": "short",
                "background": None,
            }
        ]
        valid, warnings = _validate_routines(routines, {"get_weather", "get_news", "get_calendar_events"})
        assert len(valid) == 1
        assert len(warnings) == 0

    def test_reject_unknown_commands(self) -> None:
        routines = [
            {
                "id": "bad_routine",
                "name": "Bad Routine",
                "trigger_phrases": ["bad", "nope", "wrong"],
                "steps": [
                    {"command": "nonexistent_command", "args": [], "label": "bad"},
                ],
                "response_instruction": "This won't work.",
                "response_length": "short",
                "background": None,
            }
        ]
        valid, warnings = _validate_routines(routines, {"get_weather"})
        assert len(valid) == 0
        assert any("unknown command" in w for w in warnings)

    def test_warn_on_bad_arg_keys(self) -> None:
        routines = [
            {
                "id": "test_routine",
                "name": "Test Routine",
                "trigger_phrases": ["test", "try this", "run test"],
                "steps": [
                    {"command": "get_weather", "args": "not_a_list", "label": "weather"},
                ],
                "response_instruction": "Test.",
                "response_length": "short",
                "background": None,
            }
        ]
        valid, warnings = _validate_routines(routines, {"get_weather"})
        assert len(valid) == 1
        assert any("non-list args" in w for w in warnings)

    def test_autofix_mismatched_id(self) -> None:
        routines = [
            {
                "id": "wrong_id",
                "name": "My Cool Routine",
                "trigger_phrases": ["cool", "awesome", "neat"],
                "steps": [
                    {"command": "get_weather", "args": [], "label": "weather"},
                ],
                "response_instruction": "Be cool.",
                "response_length": "medium",
                "background": None,
            }
        ]
        valid, warnings = _validate_routines(routines, {"get_weather"})
        assert len(valid) == 1
        assert valid[0]["id"] == "my_cool_routine"
        assert any("auto-fixed" in w for w in warnings)

    def test_fix_invalid_response_length(self) -> None:
        routines = [
            {
                "id": "test_routine",
                "name": "Test Routine",
                "trigger_phrases": ["test", "try", "go"],
                "steps": [
                    {"command": "get_weather", "args": [], "label": "weather"},
                ],
                "response_instruction": "Test.",
                "response_length": "extra_long",
                "background": None,
            }
        ]
        valid, warnings = _validate_routines(routines, {"get_weather"})
        assert len(valid) == 1
        assert valid[0]["response_length"] == "medium"
        assert any("invalid response_length" in w for w in warnings)

    def test_skip_empty_trigger_phrases(self) -> None:
        routines = [
            {
                "id": "no_triggers",
                "name": "No Triggers",
                "trigger_phrases": [],
                "steps": [{"command": "get_weather", "args": [], "label": "w"}],
                "response_instruction": "None.",
                "response_length": "short",
                "background": None,
            }
        ]
        valid, warnings = _validate_routines(routines, {"get_weather"})
        assert len(valid) == 0
        assert any("trigger_phrases" in w for w in warnings)

    def test_skip_empty_steps(self) -> None:
        routines = [
            {
                "id": "no_steps",
                "name": "No Steps",
                "trigger_phrases": ["a", "b", "c"],
                "steps": [],
                "response_instruction": "None.",
                "response_length": "short",
                "background": None,
            }
        ]
        valid, warnings = _validate_routines(routines, {"get_weather"})
        assert len(valid) == 0
        assert any("steps" in w for w in warnings)


# ── generate_routines integration tests ───────────────────────────────

class TestGenerateRoutines:
    @pytest.mark.asyncio
    async def test_success_with_mocked_llm(self) -> None:
        llm_response = json.dumps({
            "routines": [
                {
                    "id": "daily_update",
                    "name": "Daily Update",
                    "trigger_phrases": ["daily update", "catch me up", "what's new"],
                    "steps": [
                        {"command": "get_weather", "args": [{"key": "resolved_datetimes", "value": "today"}], "label": "weather"},
                        {"command": "get_news", "args": [{"key": "category", "value": "general"}], "label": "news"},
                    ],
                    "response_instruction": "Summarize weather and news.",
                    "response_length": "medium",
                    "background": None,
                }
            ],
            "explanation": "Created a daily update routine.",
        })

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "content": [{"text": llm_response}],
        }

        with patch("app.services.routine_generator.call_claude", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = llm_response

            result = await generate_routines(
                available_commands=SAMPLE_COMMANDS,
                llm_api_key="sk-test-key",
                model_id="haiku",
            )

        assert len(result.routines) == 1
        assert result.routines[0]["id"] == "daily_update"
        assert result.explanation == "Created a daily update routine."
        assert len(result.warnings) == 0

    @pytest.mark.asyncio
    async def test_unknown_model(self) -> None:
        with pytest.raises(ValueError, match="Unknown model"):
            await generate_routines(
                available_commands=SAMPLE_COMMANDS,
                llm_api_key="sk-test",
                model_id="nonexistent",
            )

    @pytest.mark.asyncio
    async def test_user_prompt_passed(self) -> None:
        llm_response = json.dumps({
            "routines": [],
            "explanation": "No suitable routines.",
        })

        with patch("app.services.routine_generator.call_claude", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = llm_response

            await generate_routines(
                available_commands=SAMPLE_COMMANDS,
                llm_api_key="sk-test",
                model_id="haiku",
                user_prompt="Make a workout routine",
            )

            # Verify the user prompt was included in the message
            call_args = mock_call.call_args
            messages = call_args[0][1]  # second positional arg
            assert "Make a workout routine" in messages[0]["content"]
