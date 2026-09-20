# Candidate Query Mode Benchmark

- Candidate source: curated
- Candidates sampled: 3
- Query cases: 9
- Modes: no-llm, quality
- Top-k searched: 10
- Planner cache disabled for measured runs
- Impression logging disabled for measured runs

## Overall

| mode | top1 | hit5 | hit10 | mrr10 | median rank/999 | avg ms |
|---|---:|---:|---:|---:|---:|---:|
| no-llm | 0.0 | 0.111 | 0.111 | 0.022 | 999 | 852.69 |
| quality | 0.0 | 0.222 | 0.333 | 0.094 | 999 | 4911.11 |

## By Category

### less_obvious

| mode | top1 | hit5 | hit10 | mrr10 | avg ms |
|---|---:|---:|---:|---:|---:|
| no-llm | 0.0 | 0.0 | 0.0 | 0.0 | 679.73 |
| quality | 0.0 | 0.0 | 0.0 | 0.0 | 3472.27 |

### obvious

| mode | top1 | hit5 | hit10 | mrr10 | avg ms |
|---|---:|---:|---:|---:|---:|
| no-llm | 0.0 | 0.333 | 0.333 | 0.067 | 1064.83 |
| quality | 0.0 | 0.667 | 0.667 | 0.25 | 7172.82 |

### vague

| mode | top1 | hit5 | hit10 | mrr10 | avg ms |
|---|---:|---:|---:|---:|---:|
| no-llm | 0.0 | 0.0 | 0.0 | 0.0 | 813.51 |
| quality | 0.0 | 0.0 | 0.333 | 0.033 | 4088.23 |

## Biggest Rank Deltas

| case | category | candidate | no-LLM | quality | delta | quality added skills | query |
|---|---|---|---:|---:|---:|---|---|
| c01-obvious | obvious | React Developer Candidate 12328 | - | 2 | 997 | aws, express, graphql, nextjs, node | full-stack React TypeScript developer with Docker PostgreSQL web apps |
| c01-vague | vague | React Developer Candidate 12328 | - | 10 | 989 | javascript, react, rest-api | someone who can build modern frontend product screens and connect them to backend data |
| c03-obvious | obvious | Sql Developer Candidate 12961 | 5 | 4 | 1 | data-warehousing, etl, sql-server | Microsoft SQL Server TSQL SSIS SSRS data warehousing developer |

## Case Details

### c01-obvious - obvious - React Developer Candidate 12328

Query: `full-stack React TypeScript developer with Docker PostgreSQL web apps`

Filters: `{"city": "San Francisco", "country": "USA", "skills": ["react"]}`

- no-llm: rank=-, hit5=0, results=10, 1426.88ms, top5=Software Developer Candidate 19406, Java Developer Candidate 5792, Software Developer Candidate 18975, .NET Developer Candidate 9174, Software Developer Candidate 19669
- quality: rank=2, hit5=1, results=10, 14206.52ms, top5=Software Developer Candidate 19503, React Developer Candidate 12328, Software Developer Candidate 18883, Software Developer Candidate 19727, Software Developer Candidate 19616

### c01-less-obvious - less_obvious - React Developer Candidate 12328

Query: `developer with information technology services background building full-time web applications`

Filters: `{"country": "USA"}`

- no-llm: rank=-, hit5=0, results=10, 924.61ms, top5=Software Developer Candidate 19014, Systems Administrator Candidate 25119, Java Developer Candidate 6142, Java Developer Candidate 6443, Network Administrator Candidate 8045
- quality: rank=-, hit5=0, results=10, 3396.19ms, top5=Software Developer Candidate 19581, Python Developer Candidate 14532, Project Manager Candidate 10515, Web Developer Candidate 29070, Java Developer Candidate 6142

### c01-vague - vague - React Developer Candidate 12328

Query: `someone who can build modern frontend product screens and connect them to backend data`

Filters: `{}`

- no-llm: rank=-, hit5=0, results=10, 563.17ms, top5=Systems Administrator Candidate 24725, DevOps Engineer Candidate 4011, HR Candidate 5040, Sales Candidate 5490, Sap Developer Candidate 12779
- quality: rank=10, hit5=0, results=10, 4685.97ms, top5=Java Developer Candidate 6036, Banking Candidate 862, Java Developer Candidate 10770, Python Developer Candidate 14130, Web Developer Candidate 29291

### c02-obvious - obvious - Java Developer Candidate 10757

Query: `Java J2EE Spring Boot web application developer with REST APIs`

Filters: `{"city": "Bangalore", "country": "India", "min_years_exp": 5, "skills": ["java"]}`

- no-llm: rank=-, hit5=0, results=10, 914.24ms, top5=Java Developer Candidate 6414, Web Developer Candidate 29465, Java Developer Candidate 1847, Java Developer Candidate 4869, Web Developer Candidate 29130
- quality: rank=-, hit5=0, results=10, 3420.7ms, top5=Database Administrator Candidate 2460, Python Developer Candidate 14912, Web Developer Candidate 29465, Java Developer Candidate 10787, Java Developer Candidate 10825

### c02-less-obvious - less_obvious - Java Developer Candidate 10757

Query: `engineer experienced across SDLC design development and maintenance of web based applications`

Filters: `{"country": "India"}`

- no-llm: rank=-, hit5=0, results=10, 636.59ms, top5=Security Analyst Candidate 17135, Project Manager Candidate 11187, Network Administrator Candidate 8805, Security Analyst Candidate 17454, Software Developer Candidate 19543
- quality: rank=-, hit5=0, results=10, 3578.34ms, top5=Web Developer Candidate 29811, Security Analyst Candidate 17135, Web Developer Candidate 29113, Web Developer Candidate 29936, .NET Developer Candidate 9282

### c02-vague - vague - Java Developer Candidate 10757

Query: `backend engineer for enterprise web applications with APIs and database work`

Filters: `{}`

- no-llm: rank=-, hit5=0, results=10, 1338.87ms, top5=Systems Administrator Candidate 25593, Software Developer Candidate 19607, Java Developer Candidate 5946, Security Analyst Candidate 16805, Project Manager Candidate 10622
- quality: rank=-, hit5=0, results=10, 3980.34ms, top5=Network Administrator Candidate 8510, Software Developer Candidate 19088, Network Administrator Candidate 8185, Systems Administrator Candidate 25391, Java Developer Candidate 6144

### c03-obvious - obvious - Sql Developer Candidate 12961

Query: `Microsoft SQL Server TSQL SSIS SSRS data warehousing developer`

Filters: `{"city": "Hyderabad", "country": "India", "min_years_exp": 6, "skills": ["sql"]}`

- no-llm: rank=5, hit5=1, results=10, 853.37ms, top5=Network Administrator Candidate 8398, Database Administrator Candidate 1837, Software Developer Candidate 19424, Database Administrator Candidate 2287, Sql Developer Candidate 12961
- quality: rank=4, hit5=1, results=10, 3891.24ms, top5=Etl Developer Candidate 9893, Etl Developer Candidate 9772, Software Developer Candidate 19659, Sql Developer Candidate 12961, Database Administrator Candidate 1837

### c03-less-obvious - less_obvious - Sql Developer Candidate 12961

Query: `database developer doing performance tuning troubleshooting packages reports and stored procedures`

Filters: `{"country": "India"}`

- no-llm: rank=-, hit5=0, results=5, 478.0ms, top5=Kamari Carter - Application 124, Hazel Lee - Application 3382, Shoji Fujii - Application 4665, Charlotte Green - Application 1247, Timothy Harris - Application 1616
- quality: rank=-, hit5=0, results=5, 3442.28ms, top5=Hazel Lee - Application 3382, Shoji Fujii - Application 4665, Kamari Carter - Application 124, Charlotte Green - Application 1247, Timothy Harris - Application 1616

### c03-vague - vague - Sql Developer Candidate 12961

Query: `person who can make databases faster and build reporting pipelines`

Filters: `{}`

- no-llm: rank=-, hit5=0, results=10, 538.49ms, top5=Project Manager Candidate 10801, Sql Developer Candidate 12988, Database Administrator Candidate 2017, Network Administrator Candidate 8428, Web Developer Candidate 29333
- quality: rank=-, hit5=0, results=10, 3598.37ms, top5=Sql Developer Candidate 12953, Python Developer Candidate 12212, Project Manager Candidate 10801, Systems Administrator Candidate 25495, Systems Administrator Candidate 25281
