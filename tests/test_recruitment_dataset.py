from pipeline.recruitment_dataset import transform_recruitment_row


def test_transform_recruitment_row_creates_candidate_and_resume_document():
    row = {
        "Job Applicant Name": "Daisuke Mori",
        "Age": "29",
        "Gender": "Male",
        "Race": "Mongoloid/Asian",
        "Ethnicity": "Vietnamese",
        "Resume": (
            "Proficient in Injury Prevention, Motivation, Nutrition, Health Coaching, "
            "Strength Training, with mid-level experience in fitness coaching."
        ),
        "Job Roles": "Fitness Coach",
        "Job Description": "Help clients achieve their fitness goals.",
        "Best Match": "0",
    }

    transformed = transform_recruitment_row(row, row_number=1)

    assert transformed.candidate["full_name"] == "Daisuke Mori - Application 1"
    assert transformed.candidate["email"] == "applicant-1@kaggle-recruitment.local"
    assert transformed.candidate["age"] == 29
    assert transformed.candidate["location"]
    assert transformed.candidate["city"]
    assert transformed.candidate["country"]
    assert transformed.candidate["location"] == (
        f"{transformed.candidate['city']}, {transformed.candidate['country']}"
    )
    assert 45000 <= transformed.candidate["salary_min"] <= 160000
    assert transformed.candidate["salary_max"] > transformed.candidate["salary_min"]
    assert transformed.candidate["salary_max"] <= 260000
    assert transformed.candidate["skills"] == [
        "injury prevention",
        "motivation",
        "nutrition",
        "health coaching",
        "strength training",
    ]
    assert transformed.candidate["years_exp"] == 4
    assert transformed.document["doc_type"] == "resume"
    assert transformed.document["title"] == "Resume - Fitness Coach"
    assert "Applied Role: Fitness Coach" in transformed.document["raw_text"]
    assert "Help clients achieve their fitness goals" not in transformed.document["raw_text"]
    assert "Vietnamese" not in transformed.document["raw_text"]
    assert "Mongoloid/Asian" not in transformed.document["raw_text"]
    assert transformed.eval_case["query"] == "Help clients achieve their fitness goals."
    assert transformed.eval_case["is_best_match"] is False


def test_transform_recruitment_row_handles_missing_name_and_positive_label():
    row = {
        "Job Applicant Name": "",
        "Age": "not-a-number",
        "Resume": "Proficient in Python, SQL, Machine Learning, with senior-level experience.",
        "Job Roles": "",
        "Job Description": "Build data science models.",
        "Best Match": "1",
    }

    transformed = transform_recruitment_row(row, row_number=42)

    assert transformed.candidate["full_name"] == "Candidate 42 - Application 42"
    assert transformed.candidate["age"] is None
    assert transformed.candidate["city"]
    assert transformed.candidate["country"]
    assert transformed.candidate["salary_min"] is not None
    assert transformed.candidate["salary_max"] is not None
    assert transformed.candidate["skills"] == ["python", "sql", "machine learning"]
    assert transformed.candidate["years_exp"] == 8
    assert transformed.document["title"] == "Resume - Candidate 42"
    assert transformed.eval_case["is_best_match"] is True
