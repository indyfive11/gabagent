from __future__ import annotations
import shutil
import uuid
from pathlib import Path
from gabagent.config.paths import sessions_dir
from .serializer import SessionFile


class SessionManager:
    def __init__(self, cwd: Path | None = None):
        self.cwd = cwd or Path.cwd()
        self._sessions_dir = sessions_dir(self.cwd)

    def create_session(self) -> tuple[str, SessionFile]:
        session_id = str(uuid.uuid4())
        path = self._sessions_dir / f"{session_id}.jsonl"
        path.touch()
        return session_id, SessionFile(path)

    def resume_session(self, session_id: str) -> tuple[str, SessionFile]:
        path = self._sessions_dir / f"{session_id}.jsonl"
        if not path.exists():
            raise FileNotFoundError(f"Session {session_id} not found")
        return session_id, SessionFile(path)

    def continue_last(self) -> tuple[str, SessionFile] | None:
        files = sorted(
            self._sessions_dir.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for f in files:
            if ".pre-compact" not in f.name:
                sid = f.stem
                return sid, SessionFile(f)
        return None

    def fork_session(self, session_id: str) -> tuple[str, SessionFile]:
        src = self._sessions_dir / f"{session_id}.jsonl"
        if not src.exists():
            raise FileNotFoundError(f"Session {session_id} not found")
        new_id = str(uuid.uuid4())
        dst = self._sessions_dir / f"{new_id}.jsonl"
        shutil.copy2(src, dst)
        return new_id, SessionFile(dst)

    def list_sessions(self) -> list[dict]:
        sessions = []
        for f in sorted(
            self._sessions_dir.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ):
            if ".pre-compact" in f.name:
                continue
            sf = SessionFile(f)
            msgs = sf.messages()
            first_user = next(
                (m.content for m in msgs if m.role == "user" and m.content),
                "(empty session)",
            )
            sessions.append({
                "id": f.stem,
                "mtime": f.stat().st_mtime,
                "message_count": len(msgs),
                "preview": (first_user or "")[:80],
            })
        return sessions
