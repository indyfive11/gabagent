"""Streaming filter that turns raw model token deltas into clean, speakable phrases.

Responsibilities:
  * Suppress fenced code blocks (```...```) entirely — code is never read aloud;
    instead a one-time `status` ("I've drafted some code.") is emitted per block.
  * Strip residual markdown markers (#, *, _, inline backticks, list bullets).
  * Buffer prose and flush on clause/sentence boundaries so the TTS receives whole
    phrases rather than mid-word fragments.

`feed(chunk)` and `flush()` each return a list of (kind, text) where kind is
"speak" (prose to voice) or "status" (short narration). The filter is stateful
across the whole token stream — code fences may arrive split across chunks.
"""
from __future__ import annotations
import re

_BOUNDARY = re.compile(r"[.!?;:\n]")
_MAX_BUFFER = 200  # safety flush length when no boundary appears

# Markdown cleanup applied to prose before speaking.
_MD_HEADER = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_MD_BULLET = re.compile(r"^\s{0,3}[-*+]\s+", re.MULTILINE)
_MD_EMPHASIS = re.compile(r"[*_`]+")


def _clean(text: str) -> str:
    text = _MD_HEADER.sub("", text)
    text = _MD_BULLET.sub("", text)
    text = _MD_EMPHASIS.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text


def _hold_partial_fence(s: str) -> tuple[str, str]:
    """Split off a trailing run of 1-2 backticks (a possibly-incomplete fence) so
    it isn't emitted before we know whether it completes to ``` on the next chunk.
    A run of 3+ would already have been found by find('```'), so the tail is <3."""
    i = len(s)
    while i > 0 and s[i - 1] == "`":
        i -= 1
    trailing = len(s) - i
    if 0 < trailing < 3:
        return s[:i], s[i:]
    return s, ""


class SpeakableFilter:
    def __init__(self, code_notice: str = "I've drafted some code."):
        self._pending = ""      # unprocessed raw text (incl. held partial fences)
        self._buf = ""          # prose accumulating toward a sentence boundary
        self._in_code = False
        self._code_notice = code_notice

    def feed(self, chunk: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        self._pending += chunk
        while True:
            idx = self._pending.find("```")
            if self._in_code:
                if idx == -1:
                    _, held = _hold_partial_fence(self._pending)
                    self._pending = held  # suppress code, keep a partial closing fence
                    break
                self._pending = self._pending[idx + 3:]
                self._in_code = False
                continue
            else:
                if idx == -1:
                    safe, held = _hold_partial_fence(self._pending)
                    out += self._buffer_prose(safe)
                    self._pending = held
                    break
                out += self._buffer_prose(self._pending[:idx])
                self._in_code = True
                if self._code_notice:
                    out.append(("status", self._code_notice))
                self._pending = self._pending[idx + 3:]
                continue
        return out

    def flush(self) -> list[tuple[str, str]]:
        """Emit any buffered prose at end of stream."""
        out: list[tuple[str, str]] = []
        if not self._in_code:
            out += self._buffer_prose(self._pending)
            self._pending = ""
        tail = _clean(self._buf).strip()
        self._buf = ""
        if tail:
            out.append(("speak", tail))
        return out

    # -- internal ------------------------------------------------------------
    def _buffer_prose(self, text: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        self._buf += text
        while True:
            m = _BOUNDARY.search(self._buf)
            if m:
                cut = m.end()
                phrase = _clean(self._buf[:cut]).strip()
                self._buf = self._buf[cut:]
                if phrase:
                    out.append(("speak", phrase))
                continue
            if len(self._buf) > _MAX_BUFFER:
                sp = self._buf.rfind(" ", 0, _MAX_BUFFER)
                if sp <= 0:
                    break
                phrase = _clean(self._buf[:sp]).strip()
                self._buf = self._buf[sp:]
                if phrase:
                    out.append(("speak", phrase))
                continue
            break
        return out
