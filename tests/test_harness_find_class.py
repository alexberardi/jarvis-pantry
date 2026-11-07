"""The container harness finds the package's class, not an SDK class imported beside it."""
import types

from app.services.test_harness import _find_class_in_module


def test_skips_sdk_classes_imported_into_the_module():
    base = type("IJarvisCommand", (), {"__module__": "jarvis_command_sdk.command"})
    music = type("IJarvisMusicCommand", (base,), {"__module__": "jarvis_command_sdk.music"})
    radio = type("Radio", (music,), {"__module__": "command"})
    # dir() is alphabetical: IJarvisMusicCommand sorts before Radio.
    mod = types.SimpleNamespace(IJarvisCommand=base, IJarvisMusicCommand=music, Radio=radio)
    assert _find_class_in_module(mod, base) is radio
    assert _find_class_in_module(types.SimpleNamespace(IJarvisCommand=base), base) is None
