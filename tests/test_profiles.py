from copse.profiles import _parse, list_profiles, load_profile


def test_frontmatter_values_drop_trailing_comments():
    p = _parse(
        "---\n"
        "# a whole-line comment\n"
        "name: cheap  # a cheap worker\n"
        "description: Fixes issue#12 # but not this\n"
        "provider: claude #inline\n"
        "model: haiku\t# tab before the hash\n"
        "effort: low # keep it short\n"
        "permission_mode: '#literal' # quoted values stay whole\n"
        "---\nbody\n",
        "fallback",
    )
    assert p.name == "cheap"
    assert p.description == "Fixes issue#12"  # a '#' inside a word isn't a comment
    assert p.provider == "claude"
    assert p.model == "haiku"
    assert p.effort == "low"
    assert p.permission_mode == "#literal"
    assert p.prompt == "body"


def test_lightweight_fields_parse_and_default_off():
    p = _parse(
        "---\nname: x\nstrict_mcp: true\nsetting_sources: project, local # skip user\n"
        "headless: yes\nallowed_tools: [Edit, Bash(git commit:*)]\n---\n",
        "x",
    )
    assert p.strict_mcp is True and p.headless is True
    assert p.setting_sources == ["project", "local"]
    assert p.allowed_tools == ["Edit", "Bash(git commit:*)"]

    plain = _parse("---\nname: y\nstrict_mcp: false\n---\n", "y")
    assert plain.strict_mcp is False and plain.headless is False
    assert plain.setting_sources is None and plain.effort is None


def test_builtin_profiles_keep_their_defaults():
    for p in list_profiles():
        if p.provider == "claude":
            assert not p.strict_mcp and not p.headless
            assert p.setting_sources is None and p.effort is None
    assert load_profile("developer").permission_mode == "acceptEdits"
