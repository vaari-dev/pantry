from pathlib import Path

# Nudges Whisper to write Hindi words in Latin script.
PROMPT = "Bread aur do anda, ek doodh, teen kela, dhania aur aloo. Grocery items: {items}."


class Transcriber:
    def __init__(self, model: str):
        self._model_name, self._model = model, None

    def __call__(self, audio: Path, vocabulary: list[str]) -> str:
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(self._model_name, device="cpu", compute_type="int8")
        items = ", ".join(vocabulary[:80])
        segments, _ = self._model.transcribe(
            str(audio), language="en", beam_size=5, vad_filter=True, initial_prompt=PROMPT.format(items=items)
        )
        return " ".join(s.text.strip() for s in segments).strip()
