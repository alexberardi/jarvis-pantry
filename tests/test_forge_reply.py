"""The Forge's reply format: plain text with === markers, applied to the package it revises (only
changed files come back), and the routes the browser uses to call hosted models itself."""

from app.services.forge_generator import GeneratedFile, merge_forge_reply

NEW = """=== PACKAGE ===
package_name: coin_flip
display_name: Coin Flip
=== EXPLANATION ===
Flips a coin.
=== FILE: command.py ===
def run():
    return "heads"

=== FILE: jarvis_command.yaml ===
name: coin_flip
=== END ===
"""


def _files(reply):
    return {f.filename: f.content for f in reply.result.files}


def test_new_package_gets_its_files_and_pantrys_license():
    reply = merge_forge_reply(NEW)
    files = _files(reply)
    assert reply.complete and reply.changed == ["command.py", "jarvis_command.yaml"]
    assert reply.result.package_name == "coin_flip" and reply.result.display_name == "Coin Flip"
    assert reply.result.explanation == "Flips a coin."
    # Content is kept exactly: indentation and the blank line before the next marker.
    assert files["command.py"] == 'def run():\n    return "heads"\n'
    assert files["LICENSE"].startswith("MIT License")
    assert reply.result.validation is not None


def test_a_revision_returns_only_what_it_changes():
    current = [
        GeneratedFile("command.py", "old", "python"),
        GeneratedFile("README.md", "# Coin", "markdown"),
        GeneratedFile("helpers.py", "x = 1", "python"),
        GeneratedFile("LICENSE", "MIT License (theirs)", "text"),
    ]
    text = "=== EXPLANATION ===\nFixed the sides.\n=== FILE: command.py ===\nnew\n=== DELETE: helpers.py ===\n=== END ==="
    reply = merge_forge_reply(text, current, "coin_flip", "Coin Flip")
    files = _files(reply)
    assert files == {"command.py": "new", "README.md": "# Coin", "LICENSE": "MIT License (theirs)"}
    assert reply.changed == ["command.py"] and reply.deleted == ["helpers.py"]
    # No PACKAGE section: the package keeps its names.
    assert reply.result.package_name == "coin_flip"


def test_a_cut_off_reply_is_not_complete():
    reply = merge_forge_reply("=== EXPLANATION ===\nHere it is.\n=== FILE: command.py ===\ndef run(")
    assert not reply.complete


def test_questions_are_only_an_explanation():
    reply = merge_forge_reply("=== EXPLANATION ===\nWhich broker?\n=== END ===")
    assert reply.result.files == [] and reply.result.explanation == "Which broker?"
    assert reply.result.validation is None


def test_a_reply_in_the_old_json_format_still_parses():
    text = '{"package_name": "p", "display_name": "P", "explanation": "e", "files": [{"filename": "command.py", "content": "c"}]}'
    reply = merge_forge_reply(text)
    assert _files(reply)["command.py"] == "c" and reply.complete


def test_prompt_route_hands_the_browser_the_system_prompt_and_cap(client, monkeypatch):
    monkeypatch.setattr("app.services.forge_generator._build_system_prompt", lambda tools=False: "SYSTEM+TOOLS" if tools else "SYSTEM")
    body = client.get("/v1/forge/prompt", params={"model": "sonnet"}).json()
    assert body == {"model": "sonnet", "provider": "anthropic", "api_type": "chat",
                    "api_model": "claude-sonnet-5-5", "max_tokens": 128000, "system": "SYSTEM"}
    assert client.get("/v1/forge/prompt", params={"model": "sonnet", "tools": "true"}).json()["system"] == "SYSTEM+TOOLS"
    assert client.get("/v1/forge/prompt", params={"model": "nope"}).status_code == 400


def test_parse_route_merges_the_reply(client):
    resp = client.post("/v1/forge/parse", json={
        "text": "=== FILE: command.py ===\nnew\n=== END ===",
        "current_files": [{"filename": "command.py", "content": "old"}, {"filename": "README.md", "content": "r"}],
        "package_name": "coin_flip",
    })
    body = resp.json()
    assert resp.status_code == 200 and body["complete"] and body["changed"] == ["command.py"]
    assert {f["filename"]: f["content"] for f in body["files"]}["README.md"] == "r"
    assert client.post("/v1/forge/parse", json={"text": "x" * 2_000_001}).status_code == 413
