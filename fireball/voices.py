"""Perfis biométricos locais e identificação aberta de locutores.

Só embeddings normalizados são persistidos. O áudio continua exclusivamente
na pasta original da reunião e nunca é copiado para o cadastro de vozes.
"""

from __future__ import annotations

import json
import os
import uuid
import wave
from pathlib import Path
from typing import Optional

import numpy as np

from fireball import audio, storage
from fireball.backends import BackendUnavailable, get_speaker_embedding_backend

MATCH_THRESHOLD = 0.55
MATCH_MARGIN = 0.08
MIN_ENROLL_SECONDS = 5.0
MAX_SAMPLE_SECONDS = 30.0
MAX_EMBEDDINGS_PER_PROFILE = 8


class VoiceProfileNotFound(KeyError):
    pass


class VoiceEnrollmentError(ValueError):
    pass


def profiles_root() -> Path:
    root = storage.fireball_home() / "voices"
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    return root


def _profile_path(profile_id: str) -> Path:
    if not profile_id or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-" for ch in profile_id):
        raise VoiceProfileNotFound("Perfil de voz inválido.")
    return profiles_root() / f"{profile_id}.json"


def _write_private_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        output = os.fdopen(fd, "w")
        fd = -1  # a partir daqui o objeto `output` é dono do descritor
        with output:
            json.dump(data, output, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:
        if fd >= 0:
            os.close(fd)
        tmp.unlink(missing_ok=True)
        raise


def _clean_profile(raw) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    profile_id = str(raw.get("id") or "")
    name = " ".join(str(raw.get("name") or "").split())[:80]
    model = str(raw.get("model") or "")
    embeddings = raw.get("embeddings")
    if not (profile_id and name and model and isinstance(embeddings, list) and embeddings):
        return None
    try:
        vectors = [np.asarray(item, dtype=np.float32) for item in embeddings]
    except (TypeError, ValueError):
        return None
    if not vectors or any(v.ndim != 1 or len(v) != len(vectors[0]) for v in vectors):
        return None
    centroid = _centroid(vectors)
    emails = sorted({str(email).strip().lower() for email in raw.get("emails", []) if str(email).strip()})
    return {
        "id": profile_id,
        "name": name,
        "emails": emails,
        "model": model,
        "embeddings": [v.tolist() for v in vectors[-MAX_EMBEDDINGS_PER_PROFILE:]],
        "centroid": centroid.tolist(),
        "created_at": raw.get("created_at") or storage.now_iso(),
        "updated_at": raw.get("updated_at") or storage.now_iso(),
    }


def _centroid(vectors: list["np.ndarray"]) -> "np.ndarray":
    value = np.mean(np.stack(vectors), axis=0)
    norm = float(np.linalg.norm(value))
    return value / norm if norm else value


def list_profiles() -> list[dict]:
    profiles = []
    for path in profiles_root().glob("*.json"):
        try:
            profile = _clean_profile(storage.read_json(path))
        except (OSError, json.JSONDecodeError):
            profile = None
        if profile:
            profiles.append(profile)
    return sorted(profiles, key=lambda item: item["name"].casefold())


def public_profiles() -> list[dict]:
    return [
        {
            "id": item["id"],
            "name": item["name"],
            "emails": item["emails"],
            "model": item["model"],
            "samples": len(item["embeddings"]),
            "created_at": item["created_at"],
            "updated_at": item["updated_at"],
        }
        for item in list_profiles()
    ]


def get_profile(profile_id: str) -> dict:
    profile = _clean_profile(storage.read_json(_profile_path(profile_id)))
    if not profile:
        raise VoiceProfileNotFound(f"Perfil de voz '{profile_id}' não encontrado.")
    return profile


def save_profile(
    name: str,
    embedding: "np.ndarray",
    model: str,
    email: str = "",
    profile_id: Optional[str] = None,
) -> dict:
    name = " ".join(str(name or "").split())[:80]
    if not name:
        raise VoiceEnrollmentError("Informe o nome da pessoa.")
    vector = np.asarray(embedding, dtype=np.float32)
    norm = float(np.linalg.norm(vector))
    if vector.ndim != 1 or not len(vector) or not norm:
        raise VoiceEnrollmentError("O embedding de voz produzido é inválido.")
    vector = vector / norm

    profile = None
    normalized_email = str(email or "").strip().lower()
    if profile_id:
        profile = get_profile(profile_id)

    now = storage.now_iso()
    if profile is None:
        profile = {
            "id": uuid.uuid4().hex,
            "name": name,
            "emails": [],
            "model": model,
            "embeddings": [],
            "created_at": now,
        }
    if profile["model"] != model:
        raise VoiceEnrollmentError(
            f"O perfil usa o modelo '{profile['model']}', mas o cadastro atual usa '{model}'."
        )
    vectors = [np.asarray(item, dtype=np.float32) for item in profile["embeddings"]]
    vectors.append(vector)
    vectors = vectors[-MAX_EMBEDDINGS_PER_PROFILE:]
    profile.update(
        {
            "name": name,
            "emails": sorted(set(profile["emails"] + ([normalized_email] if normalized_email else []))),
            "embeddings": [item.tolist() for item in vectors],
            "centroid": _centroid(vectors).tolist(),
            "updated_at": now,
        }
    )
    _write_private_json(_profile_path(profile["id"]), profile)
    return next(item for item in public_profiles() if item["id"] == profile["id"])


def rename_profile(profile_id: str, name: str) -> dict:
    profile = get_profile(profile_id)
    name = " ".join(str(name or "").split())[:80]
    if not name:
        raise VoiceEnrollmentError("Informe o nome da pessoa.")
    profile["name"] = name
    profile["updated_at"] = storage.now_iso()
    _write_private_json(_profile_path(profile_id), profile)
    return next(item for item in public_profiles() if item["id"] == profile_id)


def delete_profile(profile_id: str) -> dict:
    path = _profile_path(profile_id)
    if not path.exists():
        raise VoiceProfileNotFound(f"Perfil de voz '{profile_id}' não encontrado.")
    path.unlink()
    return {"id": profile_id, "deleted": True}


def _stamp(path: Path):
    """Assinatura barata de "isto mudou desde a última olhada?"."""
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


def labels_path(meeting_dir: Path) -> Path:
    return meeting_dir / "speaker_labels.json"


def load_labels(meeting_dir: Path) -> dict[str, dict]:
    raw = storage.read_json(labels_path(meeting_dir), {}) or {}
    return raw if isinstance(raw, dict) else {}


def save_label(meeting_dir: Path, speaker_key: str, assignment: dict) -> dict:
    labels = load_labels(meeting_dir)
    labels[speaker_key] = assignment
    storage.write_json(labels_path(meeting_dir), labels)
    return assignment


class VoiceMatcher:
    def __init__(self, backend=None):
        self.backend = backend or get_speaker_embedding_backend()
        self.profiles = [
            item for item in list_profiles() if item["model"] == self.backend.model
        ]

    def match(self, embedding: "np.ndarray") -> Optional[dict]:
        if not self.profiles:
            return None
        vector = np.asarray(embedding, dtype=np.float32)
        norm = float(np.linalg.norm(vector))
        if vector.ndim != 1 or not len(vector) or not norm:
            return None
        vector = vector / norm
        scores = sorted(
            (
                (float(np.dot(vector, np.asarray(profile["centroid"], dtype=np.float32))), profile)
                for profile in self.profiles
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        best_score, best = scores[0]
        second_score = scores[1][0] if len(scores) > 1 else -1.0
        if best_score < MATCH_THRESHOLD or best_score - second_score < MATCH_MARGIN:
            return None
        return {
            "profile_id": best["id"],
            "name": best["name"],
            "source": "voice",
            "confidence": round(best_score, 4),
        }

    def identify(self, audio: "np.ndarray", samplerate: int = 16000) -> Optional[dict]:
        return self.match(self.backend.embed(audio, samplerate))


def _read_wav(path: Path) -> tuple["np.ndarray", int]:
    with wave.open(str(path), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise VoiceEnrollmentError(f"Track incompatível para voz: {path.name}")
        samplerate = source.getframerate()
        frames = source.readframes(source.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0, samplerate


def _read_pcm(path: Path, samplerate: int) -> tuple["np.ndarray", int]:
    data = path.read_bytes()
    if len(data) % 2:
        data = data[:-1]  # o parecord pode estar no meio de uma amostra
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0, samplerate


def track_audio(meeting_dir: Path, track: str) -> tuple["np.ndarray", int]:
    """O áudio de uma track: o .wav quando a gravação fechou, o .pcm enquanto
    ela corre — é o que permite dar nome a uma voz no meio da reunião."""
    wav = meeting_dir / f"{track}.wav"
    if wav.exists():
        return _read_wav(wav)
    pcm = meeting_dir / f"{track}.pcm"
    if pcm.exists():
        return _read_pcm(pcm, audio.DEFAULT_SAMPLERATE)
    raise VoiceEnrollmentError(f"Esta reunião não tem áudio de {track}.")


def _slot_turns(meeting_dir: Path, track: str, speaker_id: int) -> list[dict]:
    """Onde este locutor falou.

    A passada final grava `diarization.json`, mas o tempo real já sabe disso
    antes: cada segmento de `transcript.ndjson` carrega o `speaker_key` e sua
    posição no áudio. Usar o transcript como segunda fonte é o que faz o
    cadastro funcionar durante a reunião, sem esperar a transcrição final.
    """
    diarization = storage.read_json(meeting_dir / "diarization.json", {}) or {}
    turns = [
        {"start": float(turn["start"]), "end": float(turn["end"])}
        for turn in diarization.get(track, [])
        if int(turn.get("speaker_id", -1)) == int(speaker_id)
        and float(turn.get("end", 0)) - float(turn.get("start", 0)) >= 0.8
    ]
    if turns:
        return turns

    key = f"{track}:{int(speaker_id)}"
    return [
        {"start": float(seg["start"]), "end": float(seg["end"])}
        for seg in storage.read_ndjson(meeting_dir / "transcript.ndjson")
        if seg.get("speaker_key") == key
        and seg.get("start") is not None
        and seg.get("end") is not None
        and float(seg["end"]) - float(seg["start"]) >= 0.8
    ]


def slot_audio(meeting_dir: Path, track: str, speaker_id: int) -> tuple["np.ndarray", int]:
    turns = _slot_turns(meeting_dir, track, speaker_id)
    if not turns:
        raise VoiceEnrollmentError("Não há fala limpa suficiente desse locutor.")
    samples, samplerate = track_audio(meeting_dir, track)
    chunks = []
    total = 0
    limit = int(MAX_SAMPLE_SECONDS * samplerate)
    for turn in sorted(turns, key=lambda item: float(item["end"]) - float(item["start"]), reverse=True):
        start = max(0, int(float(turn["start"]) * samplerate))
        end = min(len(samples), int(float(turn["end"]) * samplerate))
        chunk = samples[start:end]
        if len(chunk) <= 0:
            continue
        chunks.append(chunk[: max(0, limit - total)])
        total += len(chunks[-1])
        if total >= limit:
            break
    if total < MIN_ENROLL_SECONDS * samplerate:
        raise VoiceEnrollmentError(
            f"São necessários ao menos {MIN_ENROLL_SECONDS:.0f}s de fala limpa; encontrei {total / samplerate:.1f}s."
        )
    return np.concatenate(chunks), samplerate


def enroll_from_meeting(
    meeting_dir: Path,
    track: str,
    speaker_id: int,
    name: str,
    email: str = "",
    profile_id: Optional[str] = None,
) -> dict:
    if track not in ("mic", "system"):
        raise VoiceEnrollmentError("Track de voz inválida.")
    embedding, model = prepare_enrollment(meeting_dir, track, speaker_id)
    profile = save_profile(
        name=name,
        email=email,
        profile_id=profile_id,
        embedding=embedding,
        model=model,
    )
    key = f"{track}:{int(speaker_id)}"
    assignment = {
        "profile_id": profile["id"],
        "name": profile["name"],
        "source": "manual",
        "confidence": 1.0,
    }
    save_label(meeting_dir, key, assignment)
    return {"profile": profile, "speaker_key": key, "assignment": assignment}


def prepare_enrollment(
    meeting_dir: Path,
    track: str,
    speaker_id: int,
) -> tuple["np.ndarray", str]:
    """Parte pesada do cadastro, feita fora do lock global do daemon."""
    if track not in ("mic", "system"):
        raise VoiceEnrollmentError("Track de voz inválida.")
    audio, samplerate = slot_audio(meeting_dir, track, speaker_id)
    backend = get_speaker_embedding_backend()
    return backend.embed(audio, samplerate), backend.model


class LiveVoiceRecognizer:
    """Acumula poucos segundos por slot e reconhece sem aprender sozinho.

    Relê os perfis e os nomes confirmados quando eles mudam no disco: dar nome
    a uma voz no meio da reunião passa a valer para as falas seguintes, sem
    reiniciar a gravação.
    """

    def __init__(self, meeting_dir: Path, backend=None):
        self.meeting_dir = meeting_dir
        self._backend = backend
        self.matcher: Optional[VoiceMatcher] = None
        self.buffers: dict[str, list[np.ndarray]] = {}
        self.samples: dict[str, int] = {}
        self.next_attempt: dict[str, int] = {}
        self.matches = load_labels(meeting_dir)
        self._labels_stamp = _stamp(labels_path(meeting_dir))
        self._profiles_stamp = _stamp(profiles_root())
        self.has_profiles = bool(list_profiles())

    @property
    def available(self) -> bool:
        return self.has_profiles

    def _refresh(self) -> None:
        labels_stamp = _stamp(labels_path(self.meeting_dir))
        if labels_stamp != self._labels_stamp:
            self._labels_stamp = labels_stamp
            self.matches = load_labels(self.meeting_dir)
        profiles_stamp = _stamp(profiles_root())
        if profiles_stamp != self._profiles_stamp:
            self._profiles_stamp = profiles_stamp
            self.has_profiles = bool(list_profiles())
            self.matcher = None  # perfis novos: o comparador é remontado sob demanda

    def observe(
        self,
        track: str,
        speaker_id: int,
        audio: "np.ndarray",
        samplerate: int,
    ) -> Optional[dict]:
        self._refresh()
        key = f"{track}:{int(speaker_id)}"
        if key in self.matches:
            return self.matches[key]
        if not self.has_profiles:
            # sem ninguém cadastrado não há o que comparar, e guardar áudio
            # aqui só cresceria a memória do engine sem uso nenhum
            return None
        chunk = np.asarray(audio, dtype=np.float32)
        self.buffers.setdefault(key, []).append(chunk)
        self.samples[key] = self.samples.get(key, 0) + len(chunk)
        needed = self.next_attempt.get(key, int(MIN_ENROLL_SECONDS * samplerate))
        if self.samples[key] < needed:
            return None
        combined = np.concatenate(self.buffers[key])
        combined = combined[-int(MAX_SAMPLE_SECONDS * samplerate) :]
        self.buffers[key] = [combined]
        if self.matcher is None:
            self.matcher = VoiceMatcher(self._backend)
        match = self.matcher.identify(combined, samplerate)
        self.next_attempt[key] = self.samples[key] + int(3 * samplerate)
        if match:
            if any(
                other_key != key
                and other_key.startswith(f"{track}:")
                and other.get("profile_id") == match["profile_id"]
                for other_key, other in self.matches.items()
            ):
                return None
            self.matches[key] = match
            save_label(self.meeting_dir, key, match)
            self.buffers.pop(key, None)
            return match
        return None


def identify_diarized_tracks(
    meeting_dir: Path,
    diarized: dict[str, list[dict]],
) -> dict[str, dict]:
    """Reconhece slots da passada final e persiste apenas matches confiáveis."""
    previous_labels = load_labels(meeting_dir)
    manual_labels = {
        key: value
        for key, value in previous_labels.items()
        if value.get("source") == "manual"
    }
    profiles = list_profiles()
    if not profiles:
        storage.write_json(labels_path(meeting_dir), manual_labels)
        return manual_labels
    matcher = VoiceMatcher()
    candidates: dict[str, list[tuple[str, dict]]] = {}
    for track, turns in diarized.items():
        speaker_ids = sorted({int(turn["speaker_id"]) for turn in turns})
        for speaker_id in speaker_ids:
            key = f"{track}:{speaker_id}"
            try:
                audio, samplerate = slot_audio(meeting_dir, track, speaker_id)
                match = matcher.identify(audio, samplerate)
            except (VoiceEnrollmentError, BackendUnavailable):
                match = None
            if match:
                candidates.setdefault(track, []).append((key, match))

    # Um mesmo perfil não pode ocupar dois slots da mesma track. Fica com o
    # score mais alto; o outro permanece anônimo.
    labels = dict(manual_labels)
    for track, track_candidates in candidates.items():
        used = {
            value.get("profile_id")
            for key, value in labels.items()
            if key.startswith(f"{track}:") and value.get("profile_id")
        }
        for key, match in sorted(
            track_candidates, key=lambda item: item[1]["confidence"], reverse=True
        ):
            if key in manual_labels:
                continue
            if match["profile_id"] in used:
                continue
            used.add(match["profile_id"])
            previous = previous_labels.get(key)
            if previous and previous.get("profile_id") == match["profile_id"]:
                match["source"] = previous.get("source", match["source"])
            labels[key] = match
    storage.write_json(labels_path(meeting_dir), labels)
    return labels
