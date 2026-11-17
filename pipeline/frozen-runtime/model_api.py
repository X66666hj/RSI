"""Qwen transport for the existing optimizer chat interface."""


def chat(directory, name, system, payload, max_tokens=16384):
    from pathlib import Path
    from envharness_expand import ask

    return ask(Path(directory) / "calls" / name, system, payload)
