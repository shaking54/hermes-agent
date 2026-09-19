"""Deck-backed skills source: reads the primary skills tree over HTTP instead of the
filesystem, so ``skill_view``/``skills_list``/the system prompt's skills index can run
with no ``$HERMES_HOME/skills`` directory at all.

Activated by ``skills.provider: deck`` in config.yaml (parity with ``memory.provider``).
Connection details come from ``$HERMES_HOME/deck/config.json`` — the same file the deck
memory plugin reads (``deck/plugins/memory/deck/__init__.py`` in the deck repo) — plus a
skills-specific prefix. This module is deliberately independent of that plugin: the
plugin is installed separately under ``$HERMES_HOME/plugins/deck/`` and may be absent
even when skills are deck-backed, so core code cannot import it.

Only the primary ``$HERMES_HOME/skills`` tier moves to deck. Project-local dirs,
``skills.external_dirs``, plugin-provided skills, and the org mirror stay disk-based and
are merged in by the existing callers exactly as before — see ``tools/skills_tool.py``
and ``agent/prompt_builder.py``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0
DEFAULT_SKILLS_PREFIX = "skills/"
# Parity with tools/skills_tool.py's _SKILLS_CACHE_TTL_SECONDS: bounds staleness from an
# in-place SKILL.md edit on the deck side, since we don't hold a persistent connection to
# deck's change feed here.
MANIFEST_TTL_SECONDS = 30.0

# The manifest lists only these two filenames — mirrors _build_skills_manifest's disk
# walk, which drives discovery + category descriptions. Everything else (references/,
# templates/, assets/, scripts/) is fetched lazily by skill_view(name, file_path=...) on
# demand, never bulk-listed.
_INDEX_FILENAMES = ("SKILL.md", "DESCRIPTION.md")

# Org mirrors are token-gated and have no deck equivalent yet (deck has no concept of the
# ".active_org" marker or provenance sidecar) — entries under it are skipped rather than
# surfaced half-correctly. Org skills keep working from disk as before.
_ORG_MIRROR_DIR_NAME = "_org"


class DeckSkillsError(RuntimeError):
    """A deck request failed. Carries the server's message, not a traceback."""


def _load_deck_connection_config(hermes_home: str = "") -> Dict[str, Any]:
    """Read ``$HERMES_HOME/deck/config.json``, letting the environment override every key.

    Shared with the deck memory plugin's config file by design: a workspace that already
    has deck configured for memory does not need a second URL/token for skills. Env wins
    so a container can be configured without a writable home.
    """
    from hermes_constants import get_hermes_home

    cfg: Dict[str, Any] = {}
    home = hermes_home or str(get_hermes_home())
    path = Path(home) / "deck" / "config.json"
    if path.is_file():
        try:
            cfg = json.loads(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            logger.warning("deck-skills: cannot read %s: %s", path, exc)

    for key, env in (
        ("url", "DECK_URL"),
        ("workspace", "DECK_WORKSPACE"),
        ("api_key", "DECK_TOKEN"),
        ("skills_prefix", "DECK_SKILLS_PREFIX"),
    ):
        value = os.environ.get(env)
        if value:
            cfg[key] = value
    return cfg


def skills_provider_enabled() -> bool:
    """True when config.yaml sets ``skills.provider: deck``."""
    try:
        from hermes_cli.config import cfg_get, load_config

        return (cfg_get(load_config(), "skills", "provider") or "") == "deck"
    except Exception:
        logger.debug("deck-skills: could not read skills.provider", exc_info=True)
        return False


class DeckSkillsClient:
    """Thin HTTP client over deck's REST API: list/read/grep only.

    Read path only, deliberately — this module backs skill_view/skills_list/the system
    prompt index, none of which write. skill_manage (create/patch/delete a skill) staying
    disk-based, or gaining deck_write with parent-hash conflict handling, is a separate
    change.
    """

    def __init__(self, url: str, workspace: str, token: str, *, timeout: float = DEFAULT_TIMEOUT):
        import httpx
        from urllib.parse import quote

        self.workspace = workspace
        self._http = httpx.Client(
            base_url=f"{url.rstrip('/')}/ws/{quote(workspace, safe='')}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    @staticmethod
    def _escape(path: str) -> str:
        from urllib.parse import quote

        return "/".join(quote(seg, safe="") for seg in path.lstrip("/").split("/"))

    def _raise(self, resp) -> None:
        message = resp.reason_phrase or str(resp.status_code)
        try:
            body = resp.json()
            if isinstance(body, dict) and body.get("error"):
                message = body["error"]
        except Exception:
            pass
        raise DeckSkillsError(f"{resp.status_code}: {message}")

    def list(self, prefix: str, *, recursive: bool = False) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"prefix": prefix}
        if recursive:
            params["recursive"] = "1"
        resp = self._http.get("/list", params=params)
        if resp.status_code >= 300:
            self._raise(resp)
        return (resp.json() or {}).get("entries") or []

    def read(self, path: str) -> str:
        resp = self._http.get(f"/files/{self._escape(path)}")
        if resp.status_code == 404:
            raise DeckSkillsError(f"404: no such file: {path}")
        if resp.status_code >= 300:
            self._raise(resp)
        return resp.text

    def grep(self, pattern: str, prefix: str = "", *, regex: bool = False,
             ignore_case: bool = True, limit: int = 60) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"q": pattern, "max": limit}
        if prefix:
            params["path"] = prefix
        if regex:
            params["regex"] = "1"
        if ignore_case:
            params["i"] = "1"
        resp = self._http.get("/grep", params=params)
        if resp.status_code >= 300:
            self._raise(resp)
        return (resp.json() or {}).get("matches") or []

    def close(self) -> None:
        try:
            self._http.close()
        except Exception:
            pass


def _is_org_mirror_path(rel_parts: Tuple[str, ...]) -> bool:
    return bool(rel_parts) and rel_parts[0] == _ORG_MIRROR_DIR_NAME


class DeckSkillsSource:
    """Read-only view of the primary skills tier, backed by a deck workspace.

    ``manifest()`` mirrors what an ``os.walk`` + ``os.stat`` scan of ``$HERMES_HOME/skills``
    would produce for change detection (path + a signature per file); ``read()`` mirrors
    ``Path.read_text()``. Callers resolve names to paths and parse frontmatter exactly as
    they do for the local tree — only the byte source changes.
    """

    def __init__(self, config: Dict[str, Any]):
        self._config = config
        self._client: Optional[DeckSkillsClient] = None
        self._lock = threading.Lock()
        self._manifest_cache: Optional[List[Dict[str, Any]]] = None
        self._manifest_cache_at = 0.0

    @property
    def prefix(self) -> str:
        prefix = str(self._config.get("skills_prefix") or DEFAULT_SKILLS_PREFIX)
        return prefix if prefix.endswith("/") else prefix + "/"

    def _client_or_raise(self) -> DeckSkillsClient:
        if self._client is None:
            missing = [k for k in ("url", "workspace", "api_key") if not self._config.get(k)]
            if missing:
                raise DeckSkillsError(
                    "deck skills source is not configured (missing: %s) in "
                    "$HERMES_HOME/deck/config.json, or DECK_URL/DECK_WORKSPACE/DECK_TOKEN"
                    % ", ".join(missing))
            self._client = DeckSkillsClient(
                self._config["url"], self._config["workspace"], self._config["api_key"],
                timeout=float(self._config.get("timeout") or DEFAULT_TIMEOUT))
        return self._client

    def manifest(self, *, force: bool = False) -> List[Dict[str, Any]]:
        """Every ``SKILL.md`` / ``DESCRIPTION.md`` under the skills prefix, one request.

        Org-mirror entries (``skills/_org/...``) are dropped: deck has no equivalent of
        the token-gated ``.active_org`` marker, so surfacing them here would be silently
        wrong rather than absent. Org skills keep resolving from disk as today.
        """
        now = time.monotonic()
        with self._lock:
            if (not force and self._manifest_cache is not None
                    and (now - self._manifest_cache_at) < MANIFEST_TTL_SECONDS):
                return list(self._manifest_cache)
        entries = self._client_or_raise().list(self.prefix, recursive=True)
        prefix = self.prefix
        skill_files = []
        for e in entries:
            if e.get("is_dir") or e.get("name") not in _INDEX_FILENAMES:
                continue
            rel = e["path"][len(prefix):] if e["path"].startswith(prefix) else e["path"]
            if _is_org_mirror_path(tuple(rel.split("/"))):
                continue
            skill_files.append(e)
        with self._lock:
            self._manifest_cache = skill_files
            self._manifest_cache_at = now
        return list(skill_files)

    def signature(self) -> tuple:
        """Change-detection key for the prompt-builder's snapshot cache: sorted (path,
        hash) pairs — deck's own content hash stands in for the mtime signature the disk
        path uses, and is strictly more precise (mtime can be bumped without content
        changing; a deck hash cannot)."""
        return tuple(sorted((e["path"], e.get("hash", "")) for e in self.manifest()))

    def read(self, path: str) -> str:
        return self._client_or_raise().read(path)

    def list_dir(self, prefix: str, *, recursive: bool = False) -> List[Dict[str, Any]]:
        return self._client_or_raise().list(prefix, recursive=recursive)

    def relative_path(self, path: str) -> str:
        prefix = self.prefix
        return path[len(prefix):] if path.startswith(prefix) else path

    @property
    def workspace(self) -> str:
        return str(self._config.get("workspace") or "")

    def find_skill_md(self, name: str, category_hint: Optional[str] = None) -> Optional[str]:
        """Deck path of *name*'s ``SKILL.md``.

        Tries direct paths first — ``<prefix><category_hint>/SKILL.md`` (categorized
        lookup, e.g. ``"mlops/axolotl"`` from a ``plugin:skill``-shaped name that fell
        through to the local scan) then ``<prefix><name>/SKILL.md`` (flat layout) — then
        falls back to the first manifest entry whose immediate directory matches *name*.
        This is a best-effort single match, not the local resolver's full multi-root
        dedup/collision handling (``_collect_skill_candidates``): it does not match by
        frontmatter ``name:`` when that differs from the directory name, and a name that
        exists in two categories resolves to whichever the manifest lists first (sorted
        by path — deterministic, not necessarily "the right one"). Look up by
        ``category/name`` to disambiguate.
        """
        prefix = self.prefix
        matches = self.manifest()
        direct_candidates = ([f"{prefix}{category_hint}/SKILL.md"] if category_hint else [])
        direct_candidates.append(f"{prefix}{name}/SKILL.md")
        for direct in direct_candidates:
            for e in matches:
                if e["path"] == direct:
                    return direct
        fallback = sorted(
            e["path"] for e in matches
            if e["name"] == "SKILL.md" and e["path"].rsplit("/", 2)[-2] == name
        )
        return fallback[0] if fallback else None

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


_source_lock = threading.Lock()
_source: Optional[DeckSkillsSource] = None
_source_config_key: Optional[tuple] = None


def get_skills_source() -> Optional[DeckSkillsSource]:
    """The active ``DeckSkillsSource``, or ``None`` when ``skills.provider`` isn't
    ``deck`` or the connection isn't configured.

    Memoized on the resolved config so a config edit (new workspace/url) rebuilds the
    client rather than reusing a stale one. Callers (``tools/skills_tool.py``,
    ``agent/prompt_builder.py``) share this singleton so they never disagree about what
    deck is currently returning.
    """
    global _source, _source_config_key
    if not skills_provider_enabled():
        return None
    config = _load_deck_connection_config()
    missing = [k for k in ("url", "workspace", "api_key") if not config.get(k)]
    if missing:
        logger.warning(
            "deck-skills: skills.provider is 'deck' but not configured (missing: %s)",
            ", ".join(missing))
        return None
    key = tuple(sorted(config.items()))
    with _source_lock:
        if _source is None or _source_config_key != key:
            if _source is not None:
                _source.close()
            _source = DeckSkillsSource(config)
            _source_config_key = key
        return _source


def reset_skills_source() -> None:
    """Test/reload hook: drop the memoized source so the next ``get_skills_source()``
    call rebuilds it from current config."""
    global _source, _source_config_key
    with _source_lock:
        if _source is not None:
            _source.close()
        _source, _source_config_key = None, None


def _category_and_name(rel_parts: Tuple[str, ...]) -> Tuple[str, str]:
    """Same derivation as ``agent.prompt_builder._build_snapshot_entry``, applied to a
    deck-relative posix path instead of a ``Path``: ``(category, skill_name)``."""
    if len(rel_parts) < 2:
        return "general", rel_parts[0] if rel_parts else ""
    skill_name = rel_parts[-2]
    category = "/".join(rel_parts[:-2]) if len(rel_parts) > 2 else rel_parts[0]
    return category, skill_name


def build_skill_entry(path: str, prefix: str, frontmatter: Dict[str, Any], description: str) -> Dict[str, Any]:
    """Serialisable metadata dict for one deck-sourced skill — same shape as
    ``agent.prompt_builder._build_snapshot_entry`` (minus org fields, which deck-sourced
    skills never carry)."""
    from agent.skill_utils import extract_skill_conditions

    rel = path[len(prefix):] if path.startswith(prefix) else path
    rel_parts = tuple(rel.split("/"))
    category, skill_name = _category_and_name(rel_parts)
    platforms = frontmatter.get("platforms") or []
    platforms = [platforms] if isinstance(platforms, str) else platforms
    return {
        "skill_name": skill_name, "category": category,
        "frontmatter_name": str(frontmatter.get("name", skill_name)),
        "description": description,
        "platforms": [str(p).strip() for p in platforms if str(p).strip()],
        "conditions": extract_skill_conditions(frontmatter),
        "_deck_path": path,
    }


def load_skill_candidates(source: DeckSkillsSource) -> List[Tuple[Dict[str, Any], bool]]:
    """``(entry, is_compatible)`` pairs for every ``SKILL.md`` in the manifest — the
    deck-sourced equivalent of iterating ``iter_skill_index_files(skills_dir, "SKILL.md")``
    and calling ``_parse_skill_file`` on each one."""
    from agent.skill_utils import parse_frontmatter, skill_matches_environment, skill_matches_platform
    from agent.skill_utils import extract_skill_description

    prefix = source.prefix
    candidates: List[Tuple[Dict[str, Any], bool]] = []
    for e in source.manifest():
        if e.get("name") != "SKILL.md":
            continue
        try:
            content = source.read(e["path"])
            frontmatter, _ = parse_frontmatter(content)
        except Exception as exc:
            logger.warning("deck-skills: failed to read/parse %s: %s", e["path"], exc)
            continue
        is_compatible = skill_matches_platform(frontmatter) and skill_matches_environment(frontmatter)
        description = extract_skill_description(frontmatter) if is_compatible else ""
        candidates.append((build_skill_entry(e["path"], prefix, frontmatter, description), is_compatible))
    return candidates


def read_category_descriptions(source: DeckSkillsSource) -> Dict[str, str]:
    """``description`` from every ``DESCRIPTION.md`` in the manifest, keyed by category
    path — the deck-sourced equivalent of ``agent.prompt_builder._read_category_descriptions``."""
    from agent.skill_utils import parse_frontmatter

    prefix = source.prefix
    found: Dict[str, str] = {}
    for e in source.manifest():
        if e.get("name") != "DESCRIPTION.md":
            continue
        try:
            frontmatter, _ = parse_frontmatter(source.read(e["path"]))
            cat_desc = frontmatter.get("description")
        except Exception as exc:
            logger.debug("deck-skills: could not read category description %s: %s", e["path"], exc)
            continue
        if not cat_desc:
            continue
        rel = e["path"][len(prefix):] if e["path"].startswith(prefix) else e["path"]
        parts = rel.split("/")
        category = "/".join(parts[:-1]) if len(parts) > 1 else "general"
        found[category] = str(cat_desc).strip().strip("'\"")
    return found
