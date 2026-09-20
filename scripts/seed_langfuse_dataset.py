#!/usr/bin/env python3
"""
Seed Langfuse with the recruitment evaluation dataset.

Reads QUERIES from evaluate_rankers.py and uploads them to Langfuse.
"""

import os
import sys

# Ensure pipeline and scripts can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from langfuse import Langfuse
from scripts.evaluate_rankers import QUERIES

DATASET_NAME = "recruiter_golden_queries_v1"

def main():
    print(f"Connecting to Langfuse...")
    langfuse = Langfuse()
    
    print(f"Creating/updating dataset: {DATASET_NAME}")
    langfuse.create_dataset(
        name=DATASET_NAME,
        description="Golden queries and expected candidates for reranker evaluation",
    )
    
    for query in QUERIES:
        print(f"Upserting item {query.id}...")
        langfuse.create_dataset_item(
            dataset_name=DATASET_NAME,
            input={
                "query": query.query,
                "filters": {
                    "country": query.country,
                    "city": query.city,
                    "skills": query.skills,
                    "skills_match": query.skills_match,
                    "doc_types": query.doc_types,
                    "min_years_exp": query.min_years_exp,
                }
            },
            expected_output={
                "expected_name": query.expected_name,
                "expected_country": query.expected_country,
                "expected_city": query.expected_city,
                "reason": query.reason,
            },
            source_trace_id=None,
            id=query.id, # Upsert if ID is provided
        )
        
    print(f"Successfully seeded {len(QUERIES)} queries to dataset '{DATASET_NAME}'.")

if __name__ == "__main__":
    main()
