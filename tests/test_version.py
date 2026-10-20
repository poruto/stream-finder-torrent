from streamfinder.version import from_subjects


def test_features_raise_minor_others_patch():
    assert from_subjects([]) == "1.0.0"
    assert from_subjects(["chore: initial commit", "feat: add search"]) == "1.1.0"
    assert from_subjects(["feat(player): seek bar", "fix: typo", "docs: readme"]) == "1.1.2"


def test_breaking_change_raises_major():
    assert from_subjects(["feat: a", "fix: b", "feat!: new settings format"]) == "2.0.0"
    assert from_subjects(["refactor(api)!: drop old routes", "fix: c"]) == "2.0.1"


def test_every_commit_is_a_new_version():
    subjects = ["feat: a", "fix: b", "fix: c", "feat: d"]
    versions = {from_subjects(subjects[: i + 1]) for i in range(len(subjects))}
    assert len(versions) == len(subjects)
