from pipeline.resume_corpus import (
    transform_resume_atlas_row,
    transform_resume_inference_row,
)
from pipeline.resume_corpus_import import transform_source_rows


def test_transform_resume_atlas_row_extracts_candidate_fields_from_real_text():
    row = {
        "Category": "Python Developer",
        "Text": (
            "python developer pune india summary built django rest api systems. "
            "skills python django rest api mysql docker git machine learning. "
            "experience atos syntel python developer 2018 to 2022. "
            "education bachelor of engineering computer science pune university."
        ),
    }

    transformed = transform_resume_atlas_row(row, row_number=7)

    assert transformed.candidate["full_name"] == "Python Developer Candidate 7"
    assert transformed.candidate["email"] == "atlas-7@resume-corpus.local"
    assert transformed.candidate["city"] == "Pune"
    assert transformed.candidate["country"] == "India"
    assert transformed.candidate["location"] == "Pune, India"
    assert transformed.candidate["years_exp"] == 4
    assert transformed.candidate["age"] == 28
    assert transformed.candidate["skills"][:5] == [
        "python",
        "django",
        "rest-api",
        "mysql",
        "docker",
    ]
    assert transformed.candidate["salary_min"] < transformed.candidate["salary_max"]
    assert transformed.document["doc_type"] == "resume"
    assert transformed.document["title"] == "Resume - Python Developer"
    assert "Source Dataset: resume-atlas" in transformed.document["raw_text"]
    assert "Resume Text:" in transformed.document["raw_text"]
    assert transformed.document["metadata"]["source_dataset"] == "ahmedheakl/resume-atlas"
    assert transformed.document["metadata"]["source_id"] == "atlas:7"
    assert transformed.document["metadata"]["location_synthetic"] is False
    assert transformed.eval_case["query"] == "Python Developer with python django rest-api mysql docker"


def test_transform_resume_inference_row_cleans_template_leakage_and_parses_role():
    row = {
        "instruction": "Generate a Resume for a Senior Software Engineer Job",
        "input": None,
        "Resume_test": (
            "SENIOR SOFTWARE ENGINEER Professional Summary Software engineer with "
            "10 years of experience. Skills Python, JavaScript, React, Node.js, "
            "AWS, Kubernetes, PostgreSQL. Experience Company Name City State "
            "Senior Software Engineer 2014 to 2024. Education Bachelor of Science."
        ),
    }

    transformed = transform_resume_inference_row(row, row_number=3)

    assert transformed.candidate["full_name"] == "Senior Software Engineer Candidate 3"
    assert transformed.candidate["email"] == "inference-3@resume-corpus.local"
    assert transformed.candidate["years_exp"] == 10
    assert transformed.candidate["age"] == 34
    assert transformed.candidate["skills"][:6] == [
        "python",
        "javascript",
        "react",
        "node",
        "aws",
        "kubernetes",
    ]
    assert "Company Name City State" not in transformed.document["raw_text"]
    assert "[Company]" in transformed.document["raw_text"]
    assert transformed.document["metadata"]["source_dataset"] == "InferencePrince555/Resume-Dataset"
    assert transformed.document["metadata"]["source_id"] == "inference:3"
    assert transformed.eval_case["job_role"] == "Senior Software Engineer"


def test_transform_resume_inference_row_extracts_non_tech_skills_and_nearby_years_phrase():
    row = {
        "instruction": "Generate a Resume for a Accountant Job",
        "Resume_test": (
            "ACCOUNTANT Professional Summary Accounting and finance professional "
            "with 10 years extensive and diverse accounting auditing and finance "
            "experience. Knowledge of financial reporting, accruals, cost accounting, "
            "GAAP and SOX compliance. Experience analyst 1999 to 2024. "
            "Education Bachelor of Accounting."
        ),
    }

    transformed = transform_resume_inference_row(row, row_number=11)

    assert transformed.candidate["years_exp"] == 10
    assert transformed.candidate["skills"][:6] == [
        "accounting",
        "finance",
        "auditing",
        "financial-reporting",
        "cost-accounting",
        "gaap",
    ]


def test_transform_resume_inference_row_skips_empty_resume_text():
    row = {
        "instruction": "Generate a Resume for a Data Scientist Job",
        "Resume_test": None,
    }

    assert transform_resume_inference_row(row, row_number=1) is None


def test_transform_resume_atlas_row_keeps_skills_before_punctuation():
    row = {
        "Category": "Backend Developer",
        "Text": (
            "backend developer london uk skills python, mysql. "
            "experience backend developer 2020 to 2024. education bachelor. "
            "built internal APIs, reporting jobs, deployment automation, documentation, "
            "monitoring, and production support for business systems."
        ),
    }

    transformed = transform_resume_atlas_row(row, row_number=9)

    assert "mysql" in transformed.candidate["skills"]


def test_transform_resume_atlas_row_drops_resume_fragments_from_skill_block():
    row = {
        "Category": "Mechanical Engineer",
        "Text": (
            "mechanical engineer pune india profile with five years experience. "
            "skills 012015-present-nyc-teaching-classes-25-biology-chemistry-topics "
            "quick learner eagerness to learn new things competitive attitude "
            "autocad catia 3d modeling. "
            "experience engineer 2019 to 2024. education bachelor. "
            "designed mechanical components and production drawings for manufacturing teams."
        ),
    }

    transformed = transform_resume_atlas_row(row, row_number=21)

    assert transformed.candidate["skills"] == ["autocad", "catia", "3d-modeling"]


def test_transform_source_rows_applies_limit_dedup_and_role_cap():
    rows = [
        {
            "Category": "Python Developer",
            "Text": (
                "python developer pune india skills python django docker. "
                "experience company python developer 2018 to 2022. education bachelor. "
                "built APIs and data services for internal teams. "
                "owned deployment automation, monitoring, documentation, and production fixes."
            ),
        },
        {
            "Category": "Python Developer",
            "Text": (
                "python developer pune india skills python django docker. "
                "experience company python developer 2018 to 2022. education bachelor. "
                "built APIs and data services for internal teams. "
                "owned deployment automation, monitoring, documentation, and production fixes."
            ),
        },
        {
            "Category": "Python Developer",
            "Text": (
                "python developer mumbai india skills python fastapi postgresql. "
                "experience company python developer 2017 to 2022. education bachelor. "
                "built payment APIs and reporting services. "
                "owned deployment automation, monitoring, documentation, and production fixes."
            ),
        },
        {
            "Category": "Data Scientist",
            "Text": (
                "data scientist bangalore india skills python machine learning sql. "
                "experience analytics data scientist 2019 to 2023. education master. "
                "built churn models and experimentation dashboards. "
                "owned model monitoring, stakeholder reporting, documentation, and production fixes."
            ),
        },
    ]

    transformed = transform_source_rows("atlas", rows, limit=10, max_per_role=1)

    assert [item.candidate["full_name"] for item in transformed] == [
        "Python Developer Candidate 1",
        "Data Scientist Candidate 4",
    ]
