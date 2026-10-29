import sys
import threading
import types

from pantry.speech import Transcriber


def test_a_voice_note_during_the_startup_load_waits_for_it_instead_of_loading_again(monkeypatch):
    loading, release, made = threading.Event(), threading.Event(), []

    class WhisperModel:
        def __init__(self, *_, **__):
            made.append(self)
            loading.set()
            release.wait(5)

        def transcribe(self, *_, **__):
            return [types.SimpleNamespace(text=" do anda ")], None

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=WhisperModel))
    t = Transcriber("small")
    startup = threading.Thread(target=t.load)
    startup.start()
    loading.wait(5)
    heard = []
    note = threading.Thread(target=lambda: heard.append(t("note.ogg", ["anda"])))
    note.start()
    release.set()
    startup.join(5)
    note.join(5)
    assert heard == ["do anda"]
    assert len(made) == 1
