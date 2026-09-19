"""Tests for the deck-backed skills read path: agent/deck_skills.py, and its wiring into
tools/skills_tool.py (skill_view/skills_list) and agent/prompt_builder.py (the system
prompt's '## Skills' index). No real HTTP: DeckSkillsSource._client_or_raise() is
monkeypatched to a small in-memory fake standing in for deckd's REST API.
"""

import hashlib
import json

import pytest

import agent.deck_skills as deck_skills
import agent.prompt_builder as prompt_builder
import tools.skills_tool as skills_tool_module
from tools.skills_tool import _find_all_skills, skill_view, skills_list


class _FakeDeckClient:
    """Stands in for DeckSkillsClient: an in-memory {path: content} store answering the
    same list/read/grep shapes deckd's REST API returns."""

    def __init__(self, files: dict):
        self.files = dict(files)

    def list(self, prefix, *, recursive=False):
        entries = []
        seen_dirs = set()
        for path, content in self.files.items():
            if not path.startswith(prefix):
                continue
            rest = path[len(prefix):]
            if "/" in rest and not recursive:
                d = rest.split("/", 1)[0]
                if d not in seen_dirs:
                    seen_dirs.add(d)
                    entries.append({"name": d, "path": prefix + d, "is_dir": True})
                continue
            name = rest.rsplit("/", 1)[-1] if "/" in rest else rest
            entries.append({
                "name": name, "path": path, "is_dir": False,
                "hash": hashlib.sha256(content.encode()).hexdigest(),
            })
        return entries

    def read(self, path):
        if path not in self.files:
            raise deck_skills.DeckSkillsError(f"404: no such file: {path}")
        return self.files[path]

    def grep(self, *a, **kw):
        return []

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _reset_deck_skills_singleton():
    """The active DeckSkillsSource is a module-level singleton memoized on config content;
    per-test tmp HERMES_HOME dirs don't invalidate it by themselves, so drop it before and
    after every test."""
    deck_skills.reset_skills_source()
    yield
    deck_skills.reset_skills_source()


@pytest.fixture
def deck_source(monkeypatch):
    """Factory: build a DeckSkillsSource backed by *files*, and make it the one
    agent.deck_skills.get_skills_source() (and therefore skills_tool/prompt_builder) sees."""
    def _make(files):
        source = deck_skills.DeckSkillsSource({"url": "http://deck.test", "workspace": "w", "api_key": "t"})
        fake = _FakeDeckClient(files)
        monkeypatch.setattr(source, "_client_or_raise", lambda: fake)
        monkeypatch.setattr(deck_skills, "get_skills_source", lambda: source)
        return source
    return _make


AXOLOTL_SKILL = """\
---
name: axolotl
description: Fine-tune models with axolotl.
---

# axolotl

Step 1: Do the thing.
"""

WRITING_SKILL = """\
---
name: songwriting
description: Write song lyrics.
---

# songwriting

Step 1: Pick a theme.

Reference material.
"""


# ---------------------------------------------------------------------------
# DeckSkillsSource / module-level helpers
# ---------------------------------------------------------------------------

class TestDeckSkillsSource:
    def test_manifest_filters_to_index_files_and_skips_org_mirror(self, deck_source):
        source = deck_source({
            "skills/mlops/axolotl/SKILL.md": AXOLOTL_SKILL,
            "skills/mlops/axolotl/references/api.md": "# API",
            "skills/mlops/DESCRIPTION.md": "---\ndescription: ML ops skills.\n---\n",
            "skills/_org/acme/writing/SKILL.md": WRITING_SKILL,  # must be excluded
        })
        manifest_paths = {e["path"] for e in source.manifest()}
        assert manifest_paths == {"skills/mlops/axolotl/SKILL.md", "skills/mlops/DESCRIPTION.md"}

    def test_manifest_is_cached_within_ttl(self, deck_source, monkeypatch):
        source = deck_source({"skills/a/SKILL.md": AXOLOTL_SKILL})
        calls = []
        real_client = source._client_or_raise()
        monkeypatch.setattr(source, "_client_or_raise", lambda: (calls.append(1), real_client)[1])
        source.manifest()
        source.manifest()
        assert len(calls) == 1  # second call served from cache, no new client fetch

    def test_find_skill_md_flat_and_categorized(self, deck_source):
        source = deck_source({
            "skills/writing/songwriting/SKILL.md": WRITING_SKILL,
            "skills/standalone/SKILL.md": AXOLOTL_SKILL,
        })
        assert source.find_skill_md("standalone") == "skills/standalone/SKILL.md"
        assert source.find_skill_md("songwriting") == "skills/writing/songwriting/SKILL.md"
        assert source.find_skill_md("songwriting", category_hint="writing/songwriting") == \
            "skills/writing/songwriting/SKILL.md"
        assert source.find_skill_md("nope") is None

    def test_load_skill_candidates_builds_expected_entries(self, deck_source):
        source = deck_source({
            "skills/mlops/axolotl/SKILL.md": AXOLOTL_SKILL,
            "skills/standalone/SKILL.md": AXOLOTL_SKILL.replace("axolotl", "standalone", 1),
        })
        candidates = deck_skills.load_skill_candidates(source)
        by_name = {e["skill_name"]: (e, ok) for e, ok in candidates}
        entry, ok = by_name["axolotl"]
        assert ok is True
        assert entry["category"] == "mlops"
        assert entry["frontmatter_name"] == "axolotl"
        assert "Fine-tune" in entry["description"]

    def test_read_category_descriptions(self, deck_source):
        source = deck_source({
            "skills/mlops/axolotl/SKILL.md": AXOLOTL_SKILL,
            "skills/mlops/DESCRIPTION.md": "---\ndescription: ML ops skills.\n---\n",
        })
        assert deck_skills.read_category_descriptions(source) == {"mlops": "ML ops skills."}


class TestGetSkillsSource:
    def test_none_without_skills_provider_deck(self, tmp_path):
        assert deck_skills.get_skills_source() is None

    def test_none_when_provider_deck_but_unconfigured(self):
        from hermes_constants import get_hermes_home
        (get_hermes_home() / "config.yaml").write_text("skills:\n  provider: deck\n")
        assert deck_skills.get_skills_source() is None

    def test_active_when_provider_deck_and_configured(self):
        from hermes_constants import get_hermes_home
        home = get_hermes_home()
        (home / "config.yaml").write_text("skills:\n  provider: deck\n")
        (home / "deck").mkdir()
        (home / "deck" / "config.json").write_text(json.dumps(
            {"url": "http://deck.test", "workspace": "w", "api_key": "t"}))
        source = deck_skills.get_skills_source()
        assert source is not None
        assert source.prefix == "skills/"


# ---------------------------------------------------------------------------
# tools/skills_tool.py wiring
# ---------------------------------------------------------------------------

class TestSkillsToolDeckWiring:
    def test_skills_list_uses_deck_and_creates_no_local_dir(self, tmp_path, monkeypatch, deck_source):
        empty_skills_dir = tmp_path / "unused_skills"
        monkeypatch.setattr(skills_tool_module, "SKILLS_DIR", empty_skills_dir)
        deck_source({"skills/mlops/axolotl/SKILL.md": AXOLOTL_SKILL})

        result = json.loads(skills_list())

        assert result["success"] is True
        assert {s["name"] for s in result["skills"]} == {"axolotl"}
        assert result["skills"][0]["category"] == "mlops"
        assert not empty_skills_dir.exists(), \
            "deck-backed skills_list must not materialize a local skills/ directory"

    def test_find_all_skills_merges_deck_with_no_local_dirs(self, tmp_path, monkeypatch, deck_source):
        monkeypatch.setattr(skills_tool_module, "SKILLS_DIR", tmp_path / "unused")
        deck_source({
            "skills/mlops/axolotl/SKILL.md": AXOLOTL_SKILL,
            "skills/writing/songwriting/SKILL.md": WRITING_SKILL,
        })
        names = {s["name"] for s in _find_all_skills()}
        assert names == {"axolotl", "songwriting"}

    def test_skill_view_reads_primary_tier_from_deck(self, tmp_path, monkeypatch, deck_source):
        monkeypatch.setattr(skills_tool_module, "SKILLS_DIR", tmp_path / "unused")
        deck_source({"skills/mlops/axolotl/SKILL.md": AXOLOTL_SKILL})

        result = json.loads(skill_view("axolotl"))

        assert result["success"] is True
        assert result["name"] == "axolotl"
        assert "Fine-tune models" in result["description"]
        assert "Step 1: Do the thing." in result["content"]
        assert result["skill_dir"] is None
        assert result["path"] == "mlops/axolotl/SKILL.md"

    def test_skill_view_linked_file_from_deck(self, tmp_path, monkeypatch, deck_source):
        monkeypatch.setattr(skills_tool_module, "SKILLS_DIR", tmp_path / "unused")
        deck_source({
            "skills/mlops/axolotl/SKILL.md": AXOLOTL_SKILL,
            "skills/mlops/axolotl/references/api.md": "# API reference",
        })

        main = json.loads(skill_view("axolotl"))
        assert main["linked_files"] == {"references": ["references/api.md"]}

        served = json.loads(skill_view("axolotl", file_path="references/api.md"))
        assert served["success"] is True
        assert served["content"] == "# API reference"

    def test_skill_view_not_found_reports_deck_error(self, tmp_path, monkeypatch, deck_source):
        monkeypatch.setattr(skills_tool_module, "SKILLS_DIR", tmp_path / "unused")
        deck_source({"skills/mlops/axolotl/SKILL.md": AXOLOTL_SKILL})
        result = json.loads(skill_view("does-not-exist"))
        assert result["success"] is False

    def test_skill_view_disk_collision_not_masked_by_deck_fallback(
            self, tmp_path, monkeypatch, deck_source):
        """A genuine ambiguity error from the disk-based tiers (project/external dirs) must
        still surface — the deck fallback exists for 'not found', not to swallow other
        _locate_skill refusals."""
        proj_a = tmp_path / "proj_a"
        proj_b = tmp_path / "proj_b"
        for proj, content in ((proj_a, "different content A"), (proj_b, "different content B")):
            skill_dir = proj / "dup"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                f"---\nname: dup\ndescription: {content}\n---\nBody {content}\n")
        monkeypatch.setattr(skills_tool_module, "SKILLS_DIR", tmp_path / "unused")
        monkeypatch.setattr(
            "agent.skill_utils.get_project_skills_dirs", lambda: [proj_a, proj_b])
        deck_source({"skills/mlops/dup/SKILL.md": AXOLOTL_SKILL})

        result = json.loads(skill_view("dup"))
        assert result["success"] is False
        assert "Ambiguous" in result["error"]


# ---------------------------------------------------------------------------
# agent/prompt_builder.py — the '## Skills' system prompt index
# ---------------------------------------------------------------------------

class TestPromptBuilderDeckWiring:
    def test_build_skills_system_prompt_sources_primary_tier_from_deck(
            self, tmp_path, monkeypatch, deck_source):
        empty_skills_dir = tmp_path / "unused_skills"
        deck_source({
            "skills/mlops/axolotl/SKILL.md": AXOLOTL_SKILL,
            "skills/mlops/DESCRIPTION.md": "---\ndescription: ML ops skills.\n---\n",
        })
        monkeypatch.setattr("hermes_constants.get_skills_dir", lambda: empty_skills_dir)
        monkeypatch.setattr("agent.skill_utils.get_all_skills_dirs", lambda: [empty_skills_dir])
        monkeypatch.setattr("agent.skill_utils.get_project_skills_dirs", lambda: [])

        prompt = prompt_builder.build_skills_system_prompt(
            available_tools={"skill_view", "skills_list"}, available_toolsets=set())

        assert "axolotl" in prompt
        assert "Fine-tune models" in prompt
        assert "mlops: ML ops skills." in prompt
        assert not empty_skills_dir.exists()

    def test_project_skill_shadows_same_named_deck_skill(self, tmp_path, monkeypatch, deck_source):
        proj_dir = tmp_path / "proj"
        skill_dir = proj_dir / "axolotl"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: axolotl\ndescription: Project override.\n---\nProject body.\n")
        empty_skills_dir = tmp_path / "unused_skills"
        deck_source({"skills/mlops/axolotl/SKILL.md": AXOLOTL_SKILL})
        monkeypatch.setattr("hermes_constants.get_skills_dir", lambda: empty_skills_dir)
        monkeypatch.setattr("agent.skill_utils.get_all_skills_dirs", lambda: [empty_skills_dir])
        monkeypatch.setattr("agent.skill_utils.get_project_skills_dirs", lambda: [proj_dir])

        prompt = prompt_builder.build_skills_system_prompt(
            available_tools={"skill_view", "skills_list"}, available_toolsets=set())

        assert "[project] Project override." in prompt
        assert "Fine-tune models" not in prompt  # the deck copy is shadowed, not double-listed
