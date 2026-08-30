"""Transcrição final (lote) de uma reunião já gravada.

Roda como processo separado, supervisionado pelo daemon — igual ao engine, e
pelo mesmo motivo: importar whisper/parakeet custa memória e tempo, e uma
falha do backend não pode derrubar o dono do estado.

Este módulo **não mexe em meeting.json**: quem escreve status é o daemon, que
observa este processo terminar. Aqui só reescrevemos `transcript.ndjson` e
produzimos um `finalize_result.json` com o resumo que o daemon devolve ao
cliente.

A reunião tem **uma** transcrição: esta passada reescreve a do tempo real por
cima, porque é a mesma transcrição feita melhor. A troca é atômica (arquivo
temporário e `replace`) — a janela lê esse ndjson enquanto o finalize roda, e
uma troca parcial a deixaria ilegível no meio da leitura.
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
    transcript_path = meeting_dir / "transcript.ndjson"
    # escreve ao lado e troca no fim: até lá, quem lê continua vendo a
    # transcrição antiga inteira, em vez de uma meio reescrita
    new_path = meeting_dir / "transcript.ndjson.new"
    if new_path.exists():
        new_path.unlink()

    if meeting.get("fake"):
        count = 0
        for seg in storage.read_ndjson(meeting_dir / "transcript.ndjson"):
            storage.append_ndjson(new_path, seg)
            count += 1
        return _write_result(meeting_dir, new_path, count, "fake")

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
            new_path,
            {
                "seq": i,
                "start": entry["start"],
                "end": entry["end"],
                "speaker": entry["speaker"],
                "text": entry["text"],
            },
        )
    return _write_result(meeting_dir, new_path, len(entries), backend_name)


def _write_result(meeting_dir: Path, new_path: Path, segments: int, backend: str) -> dict:
    """Troca a transcrição pela recém-produzida e registra o resultado.

    A troca só acontece se saiu alguma coisa: um backend que devolveu zero
    segmento (áudio mudo, falha silenciosa) não pode apagar a transcrição do
    tempo real, que era o único registro daquela reunião.
    """
    transcript_path = meeting_dir / "transcript.ndjson"
    replaced = segments > 0
    if replaced:
        new_path.replace(transcript_path)
    else:
        new_path.unlink(missing_ok=True)

    result = {
        "transcript_path": str(transcript_path),
        "segments": segments,
        "backend": backend,
        "replaced": replaced,
    }
    storage.write_json(meeting_dir / "finalize_result.json", result)
    return result
