"""Transcrição final (lote) de uma reunião já gravada.

Roda como processo separado, supervisionado pelo daemon — igual ao engine, e
pelo mesmo motivo: importar whisper/parakeet custa memória e tempo, e uma
falha do backend não pode derrubar o dono do estado.

Este módulo **não mexe em meeting.json**: quem escreve status é o daemon, que
observa este processo terminar. Aqui só produzimos `transcript_final.ndjson` e
um `finalize_result.json` com o resumo que o daemon devolve ao cliente.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fireball import realtime, storage
from fireball.backends import get_batch_backend

TRACK_SPEAKERS = (("mic", "Você"), ("system", "Outros participantes"))


def run_finalize(meeting_dir: Path, backend: Optional[str] = None) -> dict:
    """Roda o backend de lote sobre cada .wav inteiro e mescla mic/system por
    ordem de início. Devolve (e grava) o resumo do que foi produzido."""
    meeting = storage.read_json(meeting_dir / "meeting.json")
    final_path = meeting_dir / "transcript_final.ndjson"
    if final_path.exists():
        final_path.unlink()

    if meeting.get("fake"):
        count = 0
        for seg in storage.read_ndjson(meeting_dir / "transcript.ndjson"):
            storage.append_ndjson(final_path, {**seg, "source": "final"})
            count += 1
        return _write_result(meeting_dir, final_path, count, "fake")

    backend_name = backend or meeting.get("backend") or "whisper"
    language = meeting.get("language") or realtime.DEFAULT_LANGUAGE
    batch_backend = get_batch_backend(backend_name)

    tracks = storage.read_json(meeting_dir / "audio_tracks.json", {})
    entries = []
    for key, speaker in TRACK_SPEAKERS:
        wav_path = meeting_dir / f"{key}.wav"
        if key not in tracks or not wav_path.exists():
            continue
        for seg in batch_backend.transcribe_file(wav_path, language):
            entries.append({"start": seg["start"], "end": seg["end"], "speaker": speaker, "text": seg["text"]})

    # sem timestamp (parakeet hoje), assume início da reunião — não deixa
    # sem posição pra ordenar, só perde a intercalação fina com a outra track
    entries.sort(key=lambda e: e["start"] if e["start"] is not None else 0.0)

    for i, entry in enumerate(entries, start=1):
        storage.append_ndjson(
            final_path,
            {
                "seq": i,
                "start": entry["start"],
                "end": entry["end"],
                "speaker": entry["speaker"],
                "text": entry["text"],
                "source": "final",
            },
        )
    return _write_result(meeting_dir, final_path, len(entries), backend_name)


def _write_result(meeting_dir: Path, final_path: Path, segments: int, backend: str) -> dict:
    result = {"final_path": str(final_path), "segments": segments, "backend": backend}
    storage.write_json(meeting_dir / "finalize_result.json", result)
    return result
