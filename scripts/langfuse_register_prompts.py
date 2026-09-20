#!/usr/bin/env python3
"""
Upload the local PLANNER_SYSTEM_PROMPT to Langfuse.
Run this when you change the prompt in pipeline/prompts.py.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from langfuse import Langfuse
from pipeline.prompts import PLANNER_SYSTEM_PROMPT, INSIGHTS_SYSTEM_PROMPT

def main():
    print("Connecting to Langfuse...")
    lf = Langfuse()
    
    print("Creating prompt 'planner_system_prompt'...")
    prompt = lf.create_prompt(
        name="planner_system_prompt",
        type="chat",
        prompt=[{"role": "system", "content": PLANNER_SYSTEM_PROMPT}],
        labels=["production"],
        config={
            "model": "gpt-4o",
            "temperature": 0.0,
            "max_tokens": 800,
        }
    )
    print(f"Successfully uploaded prompt 'planner_system_prompt' version {prompt.version}!")

    print("Creating prompt 'insights_system_prompt'...")
    insights_prompt = lf.create_prompt(
        name="insights_system_prompt",
        type="chat",
        prompt=[{"role": "system", "content": INSIGHTS_SYSTEM_PROMPT}],
        labels=["production"],
        config={
            "model": "gpt-4o",
            "temperature": 0.1,
            "max_tokens": 1200,
        }
    )
    print(f"Successfully uploaded prompt 'insights_system_prompt' version {insights_prompt.version}!")

if __name__ == "__main__":
    main()
