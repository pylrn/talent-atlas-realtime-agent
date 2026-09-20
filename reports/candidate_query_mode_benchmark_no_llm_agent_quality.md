# Candidate Query Mode Benchmark

- Candidate source: curated
- Candidates sampled: 7
- Query cases: 21
- Modes: no-llm, agent-quality
- Top-k searched: 10
- Planner cache disabled for measured runs
- Impression logging disabled for measured runs

## Overall

| mode | top1 | hit5 | hit10 | mrr10 | median rank/999 | avg ms |
|---|---:|---:|---:|---:|---:|---:|
| no-llm | 0.048 | 0.095 | 0.143 | 0.065 | 999 | 854.95 |
| agent-quality | 0.048 | 0.095 | 0.19 | 0.077 | 999 | 1264.78 |

## By Category

### less_obvious

| mode | top1 | hit5 | hit10 | mrr10 | avg ms |
|---|---:|---:|---:|---:|---:|
| no-llm | 0.0 | 0.0 | 0.0 | 0.0 | 819.67 |
| agent-quality | 0.0 | 0.0 | 0.0 | 0.0 | 758.7 |

### obvious

| mode | top1 | hit5 | hit10 | mrr10 | avg ms |
|---|---:|---:|---:|---:|---:|
| no-llm | 0.143 | 0.286 | 0.429 | 0.195 | 887.22 |
| agent-quality | 0.143 | 0.286 | 0.571 | 0.232 | 2019.4 |

### vague

| mode | top1 | hit5 | hit10 | mrr10 | avg ms |
|---|---:|---:|---:|---:|---:|
| no-llm | 0.0 | 0.0 | 0.0 | 0.0 | 857.97 |
| agent-quality | 0.0 | 0.0 | 0.0 | 0.0 | 1016.23 |


## Case Details

### c01-obvious - obvious - React Developer Candidate 12328

Query: `full-stack React TypeScript developer with Docker PostgreSQL web apps`

Filters: `{"city": "San Francisco", "country": "USA", "skills": ["react"]}`

- no-llm: rank=-, hit5=0, results=10, 1240.99ms, top5=Software Developer Candidate 19406, Java Developer Candidate 5792, Software Developer Candidate 18975, .NET Developer Candidate 9174, Software Developer Candidate 19669
- agent-quality: rank=-, hit5=0, results=10, 9329.84ms, top5=Software Developer Candidate 19727, Software Developer Candidate 18883, Java Developer Candidate 5792, DevOps Engineer Candidate 8962, Software Developer Candidate 19503

### c01-less-obvious - less_obvious - React Developer Candidate 12328

Query: `developer with information technology services background building full-time web applications`

Filters: `{"country": "USA"}`

- no-llm: rank=-, hit5=0, results=10, 893.04ms, top5=Software Developer Candidate 19014, Systems Administrator Candidate 25119, Java Developer Candidate 6142, Java Developer Candidate 6443, Network Administrator Candidate 8045
- agent-quality: rank=-, hit5=0, results=10, 857.45ms, top5=Software Developer Candidate 19581, Python Developer Candidate 14532, Java Developer Candidate 6142, Project Manager Candidate 10515, Software Developer Candidate 19559

### c01-vague - vague - React Developer Candidate 12328

Query: `someone who can build modern frontend product screens and connect them to backend data`

Filters: `{}`

- no-llm: rank=-, hit5=0, results=10, 618.01ms, top5=Systems Administrator Candidate 24725, DevOps Engineer Candidate 4011, HR Candidate 5040, Sales Candidate 5490, Sap Developer Candidate 12779
- agent-quality: rank=-, hit5=0, results=10, 929.21ms, top5=Security Analyst Candidate 17340, DevOps Engineer Candidate 4011, Sales Candidate 5490, Accountant Candidate 5750, HR Candidate 5040

### c02-obvious - obvious - Java Developer Candidate 10757

Query: `Java J2EE Spring Boot web application developer with REST APIs`

Filters: `{"city": "Bangalore", "country": "India", "min_years_exp": 5, "skills": ["java"]}`

- no-llm: rank=-, hit5=0, results=10, 984.74ms, top5=Java Developer Candidate 6414, Web Developer Candidate 29465, Java Developer Candidate 1847, Java Developer Candidate 4869, Web Developer Candidate 29130
- agent-quality: rank=-, hit5=0, results=10, 851.82ms, top5=Web Developer Candidate 29465, Web Developer Candidate 29466, Java Developer Candidate 10825, Web Developer Candidate 29477, Java Developer Candidate 6143

### c02-less-obvious - less_obvious - Java Developer Candidate 10757

Query: `engineer experienced across SDLC design development and maintenance of web based applications`

Filters: `{"country": "India"}`

- no-llm: rank=-, hit5=0, results=10, 600.29ms, top5=Security Analyst Candidate 17135, Project Manager Candidate 11187, Network Administrator Candidate 8805, Security Analyst Candidate 17454, Software Developer Candidate 19543
- agent-quality: rank=-, hit5=0, results=10, 738.99ms, top5=Web Developer Candidate 29811, Web Developer Candidate 29113, Security Analyst Candidate 17135, Web Developer Candidate 29936, Java Developer Candidate 6005

### c02-vague - vague - Java Developer Candidate 10757

Query: `backend engineer for enterprise web applications with APIs and database work`

Filters: `{}`

- no-llm: rank=-, hit5=0, results=10, 1583.23ms, top5=Systems Administrator Candidate 25593, Software Developer Candidate 19607, Java Developer Candidate 5946, Security Analyst Candidate 16805, Project Manager Candidate 10622
- agent-quality: rank=-, hit5=0, results=10, 1701.35ms, top5=Network Administrator Candidate 8185, Project Manager Candidate 10622, Java Developer Candidate 6144, Software Developer Candidate 18794, Network Administrator Candidate 8498

### c03-obvious - obvious - Sql Developer Candidate 12961

Query: `Microsoft SQL Server TSQL SSIS SSRS data warehousing developer`

Filters: `{"city": "Hyderabad", "country": "India", "min_years_exp": 6, "skills": ["sql"]}`

- no-llm: rank=5, hit5=1, results=10, 1129.79ms, top5=Network Administrator Candidate 8398, Database Administrator Candidate 1837, Software Developer Candidate 19424, Database Administrator Candidate 2287, Sql Developer Candidate 12961
- agent-quality: rank=1, hit5=1, results=10, 793.85ms, top5=Sql Developer Candidate 12961, Database Administrator Candidate 1837, Database Administrator Candidate 2287, Security Analyst Candidate 17454, Software Developer Candidate 19424

### c03-less-obvious - less_obvious - Sql Developer Candidate 12961

Query: `database developer doing performance tuning troubleshooting packages reports and stored procedures`

Filters: `{"country": "India"}`

- no-llm: rank=-, hit5=0, results=5, 492.67ms, top5=Kamari Carter - Application 124, Hazel Lee - Application 3382, Shoji Fujii - Application 4665, Charlotte Green - Application 1247, Timothy Harris - Application 1616
- agent-quality: rank=-, hit5=0, results=5, 577.59ms, top5=Hazel Lee - Application 3382, Shoji Fujii - Application 4665, Kamari Carter - Application 124, Charlotte Green - Application 1247, Timothy Harris - Application 1616

### c03-vague - vague - Sql Developer Candidate 12961

Query: `person who can make databases faster and build reporting pipelines`

Filters: `{}`

- no-llm: rank=-, hit5=0, results=10, 819.28ms, top5=Project Manager Candidate 10801, Sql Developer Candidate 12988, Database Administrator Candidate 2017, Network Administrator Candidate 8428, Web Developer Candidate 29333
- agent-quality: rank=-, hit5=0, results=10, 918.97ms, top5=Database Administrator Candidate 2470, Network Administrator Candidate 8428, Database Administrator Candidate 2017, Python Developer Candidate 14629, Python Developer Candidate 14332

### c04-obvious - obvious - Finance Candidate 4821

Query: `director of finance with budgeting forecasting GAAP financial reporting`

Filters: `{"city": "New York", "country": "USA", "min_years_exp": 10, "skills": ["finance"]}`

- no-llm: rank=-, hit5=0, results=10, 695.79ms, top5=Finance Candidate 4841, Finance Candidate 4856, Finance Candidate 4815, Finance Candidate 4817, Finance Candidate 4805
- agent-quality: rank=3, hit5=1, results=10, 797.31ms, top5=Finance Candidate 4805, Finance Candidate 4817, Finance Candidate 4821, Finance Candidate 4802, Finance Candidate 4868

### c04-less-obvious - less_obvious - Finance Candidate 4821

Query: `leader who aligns business initiatives with executives and builds high performance finance teams`

Filters: `{"country": "USA"}`

- no-llm: rank=-, hit5=0, results=10, 1347.85ms, top5=Finance Candidate 10103, Blockchain Candidate 3400, Software Developer Candidate 19481, Finance Candidate 10119, Project Manager Candidate 10358
- agent-quality: rank=-, hit5=0, results=10, 812.36ms, top5=Finance Candidate 10103, Systems Administrator Candidate 25460, Consultant Candidate 3729, Blockchain Candidate 3400, Management Candidate 10971

### c04-vague - vague - Finance Candidate 4821

Query: `senior person to manage company money planning controls and executive reporting`

Filters: `{}`

- no-llm: rank=-, hit5=0, results=10, 773.13ms, top5=Finance Candidate 4830, Web Developer Candidate 29425, Finance Candidate 4870, Python Developer Candidate 14459, Operations Manager Candidate 10316
- agent-quality: rank=-, hit5=0, results=10, 895.17ms, top5=Finance Candidate 4870, Operations Manager Candidate 10316, Finance Candidate 4830, Pmo Candidate 10349, Python Developer Candidate 14459

### c05-obvious - obvious - Blockchain Candidate 886

Query: `blockchain developer with Bitcoin Ethereum Solidity Hyperledger and Java`

Filters: `{"city": "Mumbai", "country": "India", "min_years_exp": 6, "skills": ["java"]}`

- no-llm: rank=-, hit5=0, results=10, 738.2ms, top5=Project Manager Candidate 11029, Python Developer Candidate 14839, Blockchain Candidate 893, Java Developer Candidate 6405, Project Manager Candidate 11187
- agent-quality: rank=-, hit5=0, results=10, 787.48ms, top5=Blockchain Candidate 893, Blockchain Candidate 881, Java Developer Candidate 6405, Blockchain Candidate 888, Blockchain Candidate 911

### c05-less-obvious - less_obvious - Blockchain Candidate 886

Query: `engineer with brain computer interface cloud computing data analytics networking and server admin`

Filters: `{"country": "India"}`

- no-llm: rank=-, hit5=0, results=0, 254.7ms, top5=
- agent-quality: rank=-, hit5=0, results=0, 262.93ms, top5=

### c05-vague - vague - Blockchain Candidate 886

Query: `engineer who understands crypto ledgers smart contracts and decentralized systems`

Filters: `{}`

- no-llm: rank=-, hit5=0, results=10, 810.36ms, top5=Blockchain Candidate 3398, Blockchain Candidate 3399, Blockchain Candidate 3387, Blockchain Candidate 3402, Blockchain Candidate 906
- agent-quality: rank=-, hit5=0, results=10, 1001.15ms, top5=Blockchain Candidate 3399, Blockchain Candidate 3395, Blockchain Candidate 3387, Blockchain Candidate 3402, Blockchain Candidate 906

### c06-obvious - obvious - Digital Media Candidate 4508

Query: `global digital marketing director with B2B marketing social media and cross functional teams`

Filters: `{"city": "Boston", "country": "USA", "min_years_exp": 8, "skills": ["marketing"]}`

- no-llm: rank=6, hit5=0, results=10, 847.39ms, top5=Public Relations Candidate 5364, Engineering Candidate 4658, Digital Media Candidate 4462, Software Developer Candidate 19597, Information Technology Candidate 5409
- agent-quality: rank=6, hit5=0, results=10, 753.82ms, top5=Engineering Candidate 4658, Public Relations Candidate 5364, Digital Media Candidate 4462, Etl Developer Candidate 9939, Software Developer Candidate 19597

### c06-less-obvious - less_obvious - Digital Media Candidate 4508

Query: `results oriented leader delivering innovation profitable measurable outcomes with collaborative teams`

Filters: `{"country": "USA"}`

- no-llm: rank=-, hit5=0, results=10, 578.71ms, top5=Project Manager Candidate 10932, Human Resources Candidate 1671, Construction Candidate 1247, Finance Candidate 1458, Health And Fitness Candidate 10221
- agent-quality: rank=-, hit5=0, results=10, 969.69ms, top5=Finance Candidate 1458, Human Resources Candidate 1671, Health And Fitness Candidate 10221, Project Manager Candidate 11304, Construction Candidate 1247

### c06-vague - vague - Digital Media Candidate 4508

Query: `person to grow digital campaigns and coordinate marketing teams`

Filters: `{}`

- no-llm: rank=-, hit5=0, results=10, 715.9ms, top5=Digital Media Candidate 4480, Fitness Candidate 4930, Web Developer Candidate 29036, Database Administrator Candidate 2041, Business Analyst Candidate 7778
- agent-quality: rank=-, hit5=0, results=10, 885.99ms, top5=Digital Media Candidate 4480, Fitness Candidate 4930, Web Developer Candidate 29036, Business Analyst Candidate 7778, Software Developer Candidate 19428

### c07-obvious - obvious - Systems Administrator Candidate 25141

Query: `Salesforce administrator developer with Visualforce Apex custom objects workflows`

Filters: `{"city": "Pune", "country": "India", "min_years_exp": 3, "skills": ["java"]}`

- no-llm: rank=1, hit5=1, results=10, 573.61ms, top5=Systems Administrator Candidate 25141, Tameka Taylor - Application 4029, Testing Candidate 2624, Software Developer Candidate 19379, React Developer Candidate 12264
- agent-quality: rank=8, hit5=0, results=10, 821.68ms, top5=React Developer Candidate 12264, Testing Candidate 2624, Tameka Taylor - Application 4029, Software Developer Candidate 19379, Database Candidate 1470

### c07-less-obvious - less_obvious - Systems Administrator Candidate 25141

Query: `CRM admin configuring formula fields validation rules approval processes page layouts and dashboards`

Filters: `{"country": "India"}`

- no-llm: rank=-, hit5=0, results=10, 1570.42ms, top5=Java Developer Candidate 6229, .NET Developer Candidate 4570, Khalil Watson - Application 2110, Civil Engineer Candidate 1194, Civil Engineer Candidate 1185
- agent-quality: rank=-, hit5=0, results=10, 1091.89ms, top5=Python Developer Candidate 14798, Khalil Watson - Application 2110, Database Administrator Candidate 1848, Civil Engineer Candidate 1194, Civil Engineer Candidate 1185

### c07-vague - vague - Systems Administrator Candidate 25141

Query: `someone to configure CRM workflows dashboards user permissions and business automations`

Filters: `{}`

- no-llm: rank=-, hit5=0, results=10, 685.87ms, top5=Database Administrator Candidate 2029, Information Technology Candidate 5417, Web Developer Candidate 29259, Sap Developer Candidate 12625, Sap Developer Candidate 12779
- agent-quality: rank=-, hit5=0, results=10, 781.75ms, top5=Network Administrator Candidate 8142, Information Technology Candidate 1773, Information Technology Candidate 5417, Sap Developer Candidate 12779, DevOps Engineer Candidate 1044
