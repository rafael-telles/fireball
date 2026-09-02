"""Catálogo SQLite das reuniões — índice derivado, arquivos continuam canônicos.

Lista e busca não precisam mais varrer `~/.fireball/meetings` nem ler o NDJSON
inteiro. O daemon (e o engine, ao gravar um segmento) atualizam este banco nas
mesmas escritas que já passam pelos arquivos. Se o índice sumir ou o schema
mudar, `rebuild()` reconstitui tudo a partir das pastas.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from fireball import storage

SCHEMA_VERSION = 1

_ANON_SPEAKER = re.compile(r"^(Você|Outros participantes|(Sala|Remoto) \d+)$")
_FTS_TOKEN = re.compile(r"\w+", re.UNICODE)


def db_path() -> Path:
    return storage.fireball_home() / "meetings.db"


@contextmanager
def _connect(rebuild_if_empty: bool = False):
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        reset = _ensure_schema(conn)
        if reset or (rebuild_if_empty and _needs_rebuild(conn)):
            _rebuild(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_schema(conn: sqlite3.Connection) -> bool:
    """Cria o schema. Devolve True se as tabelas foram recriadas (rebuild)."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == SCHEMA_VERSION:
        return False
    conn.executescript(
        """
        DROP TABLE IF EXISTS meetings_fts;
        DROP TABLE IF EXISTS segments_fts;
        DROP TABLE IF EXISTS meeting_people;
        DROP TABLE IF EXISTS meetings;
        """
    )
    conn.executescript(
        """
        CREATE TABLE meetings (
            id TEXT PRIMARY KEY,
            name TEXT,
            status TEXT,
            started_at TEXT,
            ended_at TEXT,
            tags_json TEXT NOT NULL DEFAULT '[]',
            meeting_json TEXT NOT NULL,
            segment_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE meeting_people (
            meeting_id TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL,
            PRIMARY KEY (meeting_id, name, email, source)
        );
        CREATE VIRTUAL TABLE meetings_fts USING fts5(
            meeting_id UNINDEXED,
            ident,
            name,
            tags,
            attendees,
            notes,
            summary,
            tokenize = 'unicode61 remove_diacritics 2'
        );
        CREATE VIRTUAL TABLE segments_fts USING fts5(
            meeting_id UNINDEXED,
            seq UNINDEXED,
            start UNINDEXED,
            speaker UNINDEXED,
            text,
            tokenize = 'unicode61 remove_diacritics 2'
        );
        """
    )
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return True


def _needs_rebuild(conn: sqlite3.Connection) -> bool:
    indexed = conn.execute("SELECT COUNT(*) FROM meetings").fetchone()[0]
    if indexed:
        return False
    root = storage.meetings_root()
    if not root.is_dir():
        return False
    return any(d.is_dir() and (d / "meeting.json").exists() for d in root.iterdir())


def rebuild() -> int:
    """Reconstrói o catálogo a partir das pastas. Devolve quantas reuniões entrou."""
    with _connect(rebuild_if_empty=False) as conn:
        return _rebuild(conn)


def _rebuild(conn: sqlite3.Connection) -> int:
    conn.execute("DELETE FROM segments_fts")
    conn.execute("DELETE FROM meetings_fts")
    conn.execute("DELETE FROM meeting_people")
    conn.execute("DELETE FROM meetings")
    count = 0
    root = storage.meetings_root()
    if not root.is_dir():
        return 0
    for meeting_dir in sorted(root.iterdir()):
        if meeting_dir.is_dir() and (meeting_dir / "meeting.json").exists():
            _index_meeting(conn, meeting_dir.name)
            count += 1
    return count


def _in_library(meeting_id: str) -> bool:
    """Só indexa reuniões que moram em `meetings/` — testes que gravam numa
    pasta solta não podem criar `meetings.db` na home de verdade."""
    return (storage.meetings_root() / meeting_id / "meeting.json").is_file()


def index_meeting(meeting_id: str) -> None:
    """Reindexa uma reunião inteira a partir dos arquivos."""
    if not _in_library(meeting_id):
        return
    with _connect() as conn:
        _index_meeting(conn, meeting_id)


def index_card(meeting_id: str) -> None:
    """Atualiza ficha (nome, tags, gente, notas, resumo) sem reler a transcrição."""
    if not _in_library(meeting_id):
        return
    with _connect() as conn:
        _index_card(conn, meeting_id)


def replace_transcript(meeting_id: str) -> None:
    """Substitui os segmentos indexados pelos do NDJSON atual."""
    if not _in_library(meeting_id):
        return
    with _connect() as conn:
        _replace_transcript(conn, meeting_id)
        _index_card(conn, meeting_id)


def index_segment(meeting_id: str, seg: dict) -> None:
    """Inclui (ou substitui) um segmento — caminho ao vivo do engine."""
    if not _in_library(meeting_id):
        return
    with _connect() as conn:
        _upsert_segment(conn, meeting_id, seg)
        if not _is_anon_speaker(seg.get("speaker") or ""):
            _add_person(
                conn,
                meeting_id,
                name=seg.get("speaker") or "",
                email="",
                source="speaker",
            )


def try_index_segment(meeting_id: str, seg: dict) -> None:
    """Igual a `index_segment`, mas um falha no índice não pode parar a gravação."""
    try:
        index_segment(meeting_id, seg)
    except Exception as exc:  # noqa: BLE001 — o engine continua gravando
        print(f"[catalog] segmento de {meeting_id} não indexado: {exc}", flush=True)


def remove_meeting(meeting_id: str) -> None:
    with _connect() as conn:
        _delete_meeting(conn, meeting_id)


def list_meetings() -> list[dict]:
    """Todas as reuniões, mais antigas primeiro (como a ordem de pasta)."""
    with _connect(rebuild_if_empty=True) as conn:
        rows = conn.execute(
            "SELECT meeting_json FROM meetings ORDER BY started_at ASC, id ASC"
        ).fetchall()
    return [json.loads(row["meeting_json"]) for row in rows]


def meeting_summaries() -> list[dict]:
    """Histórico da GUI: mais recentes primeiro, com contagem de segmentos."""
    with _connect(rebuild_if_empty=True) as conn:
        rows = conn.execute(
            """
            SELECT meeting_json, segment_count
            FROM meetings
            ORDER BY started_at DESC, id DESC
            """
        ).fetchall()
    out = []
    for row in rows:
        meeting = json.loads(row["meeting_json"])
        meeting["segments"] = row["segment_count"]
        out.append(meeting)
    return out


def search(query: str, limit: int = 50, segments_per_meeting: int = 5) -> dict:
    """Busca FTS em ficha e transcrição, agrupada por reunião."""
    match = _fts_query(query)
    if not match:
        return {"query": query, "meetings": []}

    with _connect(rebuild_if_empty=True) as conn:
        card_hits = _search_cards(conn, match, limit)
        seg_hits = _search_segments(conn, match, limit * segments_per_meeting)

        by_id: dict[str, dict] = {}
        order: list[str] = []

        def bucket(meeting_id: str) -> Optional[dict]:
            if meeting_id in by_id:
                return by_id[meeting_id]
            row = conn.execute(
                "SELECT meeting_json, segment_count FROM meetings WHERE id = ?",
                (meeting_id,),
            ).fetchone()
            if row is None:
                return None
            meeting = json.loads(row["meeting_json"])
            item = {
                **meeting,
                "segments": row["segment_count"],
                "matched": [],
                "snippet": "",
                "hits": [],
            }
            by_id[meeting_id] = item
            order.append(meeting_id)
            return item

        for hit in card_hits:
            item = bucket(hit["meeting_id"])
            if item is None:
                continue
            for field in hit["matched"]:
                if field not in item["matched"]:
                    item["matched"].append(field)
            if hit["snippet"] and not item["snippet"]:
                item["snippet"] = hit["snippet"]

        for hit in seg_hits:
            item = bucket(hit["meeting_id"])
            if item is None:
                continue
            if "transcript" not in item["matched"]:
                item["matched"].append("transcript")
            if len(item["hits"]) < segments_per_meeting:
                item["hits"].append(
                    {
                        "seq": hit["seq"],
                        "start": hit["start"],
                        "speaker": hit["speaker"],
                        "snippet": hit["snippet"],
                    }
                )
            if not item["snippet"]:
                item["snippet"] = hit["snippet"]

        meetings = [by_id[mid] for mid in order][:limit]
    return {"query": query, "meetings": meetings}


def _search_cards(conn: sqlite3.Connection, match: str, limit: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            meeting_id,
            rank,
            snippet(meetings_fts, 1, '', '', '…', 8) AS ident_snip,
            snippet(meetings_fts, 2, '', '', '…', 8) AS name_snip,
            snippet(meetings_fts, 3, '', '', '…', 8) AS tags_snip,
            snippet(meetings_fts, 4, '', '', '…', 10) AS attendees_snip,
            snippet(meetings_fts, 5, '', '', '…', 12) AS notes_snip,
            snippet(meetings_fts, 6, '', '', '…', 12) AS summary_snip
        FROM meetings_fts
        WHERE meetings_fts MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (match, limit),
    ).fetchall()
    fields = (
        ("ident_snip", "id"),
        ("name_snip", "name"),
        ("tags_snip", "tags"),
        ("attendees_snip", "attendees"),
        ("notes_snip", "notes"),
        ("summary_snip", "summary"),
    )
    hits = []
    for row in rows:
        matched = [name for col, name in fields if _snip_hit(row[col])]
        snippet = next((row[col].strip() for col, _name in fields if _snip_hit(row[col])), "")
        hits.append(
            {
                "meeting_id": row["meeting_id"],
                "matched": matched,
                "snippet": snippet,
                "rank": row["rank"],
            }
        )
    return hits


def _search_segments(conn: sqlite3.Connection, match: str, limit: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            meeting_id,
            seq,
            start,
            speaker,
            snippet(segments_fts, 4, '', '', '…', 12) AS snippet,
            rank
        FROM segments_fts
        WHERE segments_fts MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (match, limit),
    ).fetchall()
    return [
        {
            "meeting_id": row["meeting_id"],
            "seq": int(row["seq"]) if row["seq"] not in (None, "") else 0,
            "start": _parse_start(row["start"]),
            "speaker": row["speaker"] or "",
            "snippet": (row["snippet"] or "").strip(),
            "rank": row["rank"],
        }
        for row in rows
    ]


def _snip_hit(value) -> bool:
    text = (value or "").strip()
    return bool(text) and text != "…"


def _parse_start(value) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fts_query(raw: str) -> str:
    tokens = _FTS_TOKEN.findall(raw or "")
    parts = []
    for token in tokens:
        token = token.replace('"', "")
        if token:
            parts.append(f'"{token}"*')
    return " AND ".join(parts)


def _index_meeting(conn: sqlite3.Connection, meeting_id: str) -> None:
    meeting_dir = storage.meetings_root() / meeting_id
    if not (meeting_dir / "meeting.json").exists():
        _delete_meeting(conn, meeting_id)
        return
    _index_card(conn, meeting_id)
    _replace_transcript(conn, meeting_id)
    # a ficha FTS inclui os nomes da transcrição; eles só existem depois do NDJSON
    _index_card(conn, meeting_id)


def _index_card(conn: sqlite3.Connection, meeting_id: str) -> None:
    meeting_dir = storage.meetings_root() / meeting_id
    meeting = storage.read_json(meeting_dir / "meeting.json")
    if not meeting:
        return
    meeting.setdefault("id", meeting_id)
    tags = [str(tag) for tag in (meeting.get("tags") or []) if tag]
    notes = _read_text(meeting_dir / "notes.md")
    summary = _read_text(meeting_dir / "summary.md")
    people = _people_from_event(meeting.get("event"))
    speakers = conn.execute(
        "SELECT name FROM meeting_people WHERE meeting_id = ? AND source = 'speaker'",
        (meeting_id,),
    ).fetchall()
    attendee_names = [p["name"] or p["email"] for p in people]
    attendee_names.extend(row["name"] for row in speakers if row["name"])
    attendees_text = " ".join(dict.fromkeys(n for n in attendee_names if n))

    conn.execute(
        """
        INSERT INTO meetings (id, name, status, started_at, ended_at, tags_json, meeting_json, segment_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(
            (SELECT segment_count FROM meetings WHERE id = ?), 0
        ))
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            status = excluded.status,
            started_at = excluded.started_at,
            ended_at = excluded.ended_at,
            tags_json = excluded.tags_json,
            meeting_json = excluded.meeting_json
        """,
        (
            meeting_id,
            meeting.get("name"),
            meeting.get("status"),
            meeting.get("started_at"),
            meeting.get("ended_at"),
            json.dumps(tags, ensure_ascii=False),
            json.dumps(meeting, ensure_ascii=False),
            meeting_id,
        ),
    )
    conn.execute(
        "DELETE FROM meeting_people WHERE meeting_id = ? AND source = 'attendee'",
        (meeting_id,),
    )
    for person in people:
        _add_person(conn, meeting_id, person["name"], person["email"], "attendee")

    conn.execute("DELETE FROM meetings_fts WHERE meeting_id = ?", (meeting_id,))
    conn.execute(
        """
        INSERT INTO meetings_fts (meeting_id, ident, name, tags, attendees, notes, summary)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            meeting_id,
            meeting_id,
            meeting.get("name") or "",
            " ".join(tags),
            attendees_text,
            notes,
            summary,
        ),
    )


def _replace_transcript(conn: sqlite3.Connection, meeting_id: str) -> None:
    conn.execute("DELETE FROM segments_fts WHERE meeting_id = ?", (meeting_id,))
    conn.execute(
        "DELETE FROM meeting_people WHERE meeting_id = ? AND source = 'speaker'",
        (meeting_id,),
    )
    path = storage.meetings_root() / meeting_id / "transcript.ndjson"
    count = 0
    speakers: dict[str, None] = {}
    for seg in storage.read_ndjson(path):
        _upsert_segment(conn, meeting_id, seg, bump_count=False)
        count += 1
        name = (seg.get("speaker") or "").strip()
        if name and not _is_anon_speaker(name):
            speakers[name] = None
    for name in speakers:
        _add_person(conn, meeting_id, name, "", "speaker")
    conn.execute(
        "UPDATE meetings SET segment_count = ? WHERE id = ?",
        (count, meeting_id),
    )


def _upsert_segment(
    conn: sqlite3.Connection, meeting_id: str, seg: dict, bump_count: bool = True
) -> None:
    seq = seg.get("seq")
    if seq is None:
        return
    existed = conn.execute(
        "SELECT rowid FROM segments_fts WHERE meeting_id = ? AND seq = ?",
        (meeting_id, str(seq)),
    ).fetchone()
    if existed:
        conn.execute(
            "DELETE FROM segments_fts WHERE meeting_id = ? AND seq = ?",
            (meeting_id, str(seq)),
        )
    conn.execute(
        """
        INSERT INTO segments_fts (meeting_id, seq, start, speaker, text)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            meeting_id,
            str(seq),
            "" if seg.get("start") is None else str(seg.get("start")),
            seg.get("speaker") or "",
            seg.get("text") or "",
        ),
    )
    if bump_count and not existed:
        exists = conn.execute("SELECT 1 FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        if exists:
            conn.execute(
                "UPDATE meetings SET segment_count = segment_count + 1 WHERE id = ?",
                (meeting_id,),
            )
        else:
            conn.execute(
                """
                INSERT INTO meetings (id, name, status, started_at, ended_at, tags_json, meeting_json, segment_count)
                VALUES (?, NULL, NULL, NULL, NULL, '[]', ?, 1)
                """,
                (meeting_id, json.dumps({"id": meeting_id})),
            )


def _delete_meeting(conn: sqlite3.Connection, meeting_id: str) -> None:
    conn.execute("DELETE FROM segments_fts WHERE meeting_id = ?", (meeting_id,))
    conn.execute("DELETE FROM meetings_fts WHERE meeting_id = ?", (meeting_id,))
    conn.execute("DELETE FROM meeting_people WHERE meeting_id = ?", (meeting_id,))
    conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))


def _add_person(conn: sqlite3.Connection, meeting_id: str, name: str, email: str, source: str) -> None:
    name = (name or "").strip()
    email = (email or "").strip()
    if not name and not email:
        return
    conn.execute(
        """
        INSERT OR IGNORE INTO meeting_people (meeting_id, name, email, source)
        VALUES (?, ?, ?, ?)
        """,
        (meeting_id, name, email, source),
    )


def _people_from_event(event) -> list[dict]:
    people = []
    if not isinstance(event, dict):
        return people
    for person in event.get("attendees") or []:
        if not isinstance(person, dict):
            continue
        name = (person.get("name") or "").strip()
        email = (person.get("email") or "").strip()
        if name or email:
            people.append({"name": name, "email": email})
    organizer = event.get("organizer")
    if isinstance(organizer, dict):
        name = (organizer.get("name") or "").strip()
        email = (organizer.get("email") or "").strip()
        if name or email:
            people.append({"name": name, "email": email})
    return people


def _is_anon_speaker(name: str) -> bool:
    return not name or bool(_ANON_SPEAKER.match(name.strip()))


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text()
