from agents.calibrate_reviews import passes


def test_regression_requires_the_seeded_defect_not_any_issue():
    unrelated = {"score": 70, "issues": [
        {"type": "terminology", "target_excerpt": "deductible"},
    ]}
    assert not passes("changed-amount", unrelated)
    correct = {"score": 70, "issues": [{"type": "number", "target_excerpt": "EUR 5000.00"}]}
    assert passes("changed-amount", correct)
    correct["issues"].append({"type": "formatting", "target_excerpt": "EUR 500.00"})
    assert not passes("changed-amount", correct)


def test_clean_translation_requires_no_issues_and_passing_score():
    assert passes("accurate-localized", {"score": 100, "issues": []})
    assert not passes("accurate-localized", {"score": 60, "issues": []})
    assert not passes("accurate-localized", {"score": 95, "issues": [{"type": "formatting"}]})
