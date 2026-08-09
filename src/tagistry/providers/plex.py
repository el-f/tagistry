"""Plex sink: refresh the music library after Tagistry edits tags/art on disk.

Plex keeps showing stale metadata until it rescans. This is NOT a tag ground-truth provider
(Plex derives its metadata from the same sources Tagistry already uses) — it's a post-apply
companion that tells Plex to pick up the changes. Needs a server URL + token. HTTP callables
are injected so tests stay hermetic.
"""

from __future__ import annotations

from collections.abc import Callable

type JsonGetter = Callable[[str], dict[str, object] | None]
type Hitter = Callable[[str], bool]  # url -> True on a 2xx


class Plex:
    def __init__(self, base_url: str, token: str, json_getter: JsonGetter, hitter: Hitter) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._json = json_getter
        self._hit = hitter

    def _url(self, path: str) -> str:
        sep = "&" if "?" in path else "?"
        return f"{self._base}{path}{sep}X-Plex-Token={self._token}"

    def music_sections(self) -> list[str]:
        """Section keys of every music (artist) library."""
        data = self._json(self._url("/library/sections"))
        if not data:
            return []
        container = data.get("MediaContainer")
        directories = container.get("Directory") if isinstance(container, dict) else None
        if not isinstance(directories, list):
            return []
        return [str(d["key"]) for d in directories if isinstance(d, dict) and d.get("type") == "artist" and "key" in d]

    def refresh(self, section_id: str) -> bool:
        return self._hit(self._url(f"/library/sections/{section_id}/refresh"))

    def refresh_music(self) -> int:
        """Trigger a scan on every music section. Returns how many were refreshed."""
        return sum(1 for section in self.music_sections() if self.refresh(section))


def default_plex(base_url: str, token: str, timeout: int = 15) -> Plex:
    import requests

    def json_getter(url: str) -> dict[str, object] | None:
        try:
            resp = requests.get(url, timeout=timeout, headers={"Accept": "application/json"})
            if resp.status_code != 200:
                return None
            data: dict[str, object] = resp.json()
            return data
        except (requests.RequestException, ValueError):
            return None

    def hitter(url: str) -> bool:
        try:
            return requests.get(url, timeout=timeout).status_code // 100 == 2
        except requests.RequestException:
            return False

    return Plex(base_url, token, json_getter, hitter)
