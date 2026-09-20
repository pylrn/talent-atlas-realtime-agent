# New System Pipeline — Full Visual

Renders in VS Code (Mermaid preview) and GitHub. Colour legend:

- 🟨 **LLM** (reads/produces language — agent, planner LLM, sub-LLM tools)
- 🟩 **Deterministic** (one correct answer, no language — rules, SQL, ranking math)
- 🟦 **UI / transport**

---

## 1. Master diagram — every connection

```mermaid
flowchart TD
  %% ===================== ENTRY =====================
  subgraph UI["🖥️ UI entry points"]
    A1["Agent search bar (primary)"]
    A2["Specialised search · Query"]
    A3["Specialised search · JD"]
    A4["Advanced options / chips"]
  end

  %% ============== PRE-FLIGHT SHORTCUTS ==============
  subgraph PRE["⚡ Pre-flight structural shortcuts (rules, no LLM)"]
    P1{"empty text + chips set?"}
    P2{"exact greeting / thanks?"}
  end

  A1 --> P1
  A4 --> P1
  P1 -- yes --> FO["filter_only search (skip agent + planner)"]
  P1 -- no --> P2
  P2 -- yes --> MET["templated reply (no tool, no agent)"]
  P2 -- no --> AG
  A2 --> SEARCHEP["/search endpoint"]
  A3 --> SEARCHEP

  %% ===================== AGENT =====================
  subgraph AGENT["🤖 recruiter_agent — DeepSeek deepseek-v4-flash"]
    AG["Agent loop · agent.iter()"]
    SP["System prompt:<br/>memory block · intent-routing few-shot (soft router)<br/>how-to examples · on-screen context"]
    SP -. guides .-> AG
  end

  %% ===================== TOOLS =====================
  subgraph TOOLS["🧰 Tools — agent picks ONE per step"]
    subgraph T_SEARCH["Search"]
      RS["run_search"]
      MS["modify_and_search"]
      KS["keyword_search"]
    end
    subgraph T_INSPECT["Inspect"]
      VR["view_current_results"]
      GC["get_candidate_detail"]
      EP["explain_poor_results"]
      CI["compare_iterations"]
      CC["compare_candidates"]
    end
    subgraph T_ANALYTICS["Analytics"]
      QDB["query_candidates_db"]
      LP["load_candidate_pool"]
      FP["filter_from_pool"]
      AP["aggregate_pool"]
      RP["rerank_pool"]
    end
    subgraph T_ACTION["Actions"]
      AJ["analyze_jd"]
      DO["draft_outreach"]
      IQ["generate_interview_questions"]
      US["update_shortlist"]
      UWS["update_working_spec"]
      PM["push_to_main_panel"]
      SV["save_search"]
      ES["export_shortlist"]
    end
    subgraph T_MEMORY["Memory"]
      SH["save_hint"]
      AO["add_observation"]
      CO["confirm_observation"]
    end
  end

  AG --> RS & MS & KS
  AG --> VR & GC & EP & CI & CC
  AG --> QDB & LP & FP & AP & RP
  AG --> AJ & DO & IQ & US & UWS & PM & SV & ES
  AG --> SH & AO & CO

  %% ================ SEARCH PIPELINE ================
  subgraph SE["🔎 smart_search — deterministic core"]
    S0{"empty text + filters?"}
    S0 -- yes --> SFO["_filter_only_search (SQL, no LLM)"]
    S0 -- no --> RD["route detect"]
    RD --> PLAN
    subgraph PLAN["🧭 planner._plan"]
      G1["sanitize"] --> G2{"simple query? ≤4 tok, no NL"}
      G2 -- yes --> G3["regex parse (no LLM)"]
      G2 -- no --> G4{"cache hit?"}
      G4 -- yes --> G5["cached spec"]
      G4 -- no --> G6["LLM call → validate → normalize → cache"]
      G6 -. on fail .-> G3
    end
    PLAN --> CG{"confidence gate"}
    CG -- low --> CL["clarify notice"]
    CG -- ok --> RET
    subgraph RET["retrieval + ranking"]
      R1["dense"] --> RRF["RRF merge"]
      R2["bm25"] --> RRF
      R3["skill"] --> RRF
      RRF --> XE["cross-encoder rerank"]
      XE --> FSC["8-signal feature score"]
      FSC --> MMR["MMR diversity"]
    end
  end

  RS --> S0
  MS --> S0
  SEARCHEP --> S0
  FO --> SFO

  %% ============== RESULTS + RECOVERY ==============
  MMR --> RESP["SearchResponse (results + spec_summary)"]
  G3 --> RET
  G5 --> RET
  SFO --> RESP
  CL --> RESP
  RESP --> REC{"empty or low score?"}
  REC -- yes --> AUTO["auto-attach explain_poor_results + relax suggestions"]
  AUTO --> AG
  REC -- no --> CHIPS

  %% ===================== OUTPUT =====================
  subgraph OUT["📤 Output — SSE stream"]
    CHIPS["refinement chips + next-action suggestions"]
    CARDS["result cards"]
    TXT["agent text + closing line"]
  end
  RESP --> CARDS
  AG --> TXT
  TXT --> UIOUT["UI render"]
  CARDS --> UIOUT
  CHIPS --> UIOUT

  %% ================== PERSISTENCE ==================
  subgraph DBP["🗄️ Postgres"]
    MEM["recruiter_memory (hints + observations)"]
    HIST["search_history"]
    IMP["search_impressions"]
  end
  SH --> MEM
  AO --> MEM
  CO --> MEM
  MEM -. personalization hints .-> G6
  SV --> HIST
  RESP --> IMP

  %% ================= OBSERVABILITY =================
  OBS["📊 Langfuse spans<br/>agent.respond · tool.* · search.plan.* · search.retrieve.*"]
  AG -. traces .-> OBS
  SE -. traces .-> OBS

  %% ===================== STYLES =====================
  classDef llm fill:#fde68a,stroke:#d97706,color:#111;
  classDef det fill:#bbf7d0,stroke:#16a34a,color:#111;
  classDef ui  fill:#bfdbfe,stroke:#2563eb,color:#111;
  class AG,G6,AJ,DO,IQ,CC llm;
  class FO,MET,P1,P2,SFO,S0,RD,G1,G2,G3,G4,G5,RRF,XE,FSC,MMR,R1,R2,R3,CG,AUTO,REC,CHIPS,QDB,LP,FP,AP,KS,SH,AO,CO,US,UWS,PM,SV,ES,VR,GC,EP,CI det;
  class A1,A2,A3,A4,UIOUT,CARDS,TXT,SEARCHEP ui;
```

---

## 2. The same flow at a glance (ASCII)

```
                    ┌──────────────── UI ENTRY ─────────────────┐
   agent bar ──┐    │  Specialised·Query   Specialised·JD       │
   chips ──────┤    └──────────┬──────────────────┬────────────┘
               ▼               ▼                  ▼
   ⚡ PRE-FLIGHT (rules)    /search ──────────────┐│
   empty+chips? ─yes─► filter_only ───────────────┼┼──► smart_search
   greeting?    ─yes─► templated reply (no agent) ││         │
        │ no                                       ││         │
        ▼                                          ││         │
   🤖 AGENT (DeepSeek)  ◄── system prompt          ││         │
        │  (memory + intent few-shot + examples)   ││         │
        │  picks ONE tool                          ││         │
        ▼                                          ││         │
   🧰 TOOLS  ── Search ─► run/modify ──────────────┘│         │
              ├ Inspect  (view, detail, explain, compare)     │
              ├ Analytics(db, pool, aggregate, keyword)        │
              ├ Actions  (jd, outreach, interview, shortlist…) │
              └ Memory   (save_hint, observations) ─► Postgres │
                                                                ▼
   🔎 smart_search:  empty+filters? ─yes─► SQL filter_only      │
        else ► route ► planner[sanitize→simple?→regex|cache|LLM→validate]
             ► confidence gate ► dense+bm25+skill →RRF→cross-enc→8-signal→MMR
                                                                │
        ▼                                                       ▼
   📤 SearchResponse ─► empty/low? ─yes─► auto-diagnose ─► back to AGENT
        │  no                                                   
        ▼                                                       
   result cards + refinement chips + agent closing line ─► UI
        │
   📊 every step traced to Langfuse   🗄️ memory feeds planner personalization
```

---

## 3. What each colour means for your original question

- 🟨 **LLM nodes** are where you *let the model decide*: the agent's tool choice
  (guided softly by the few-shot intent examples), the planner's semantic parse, and
  the generative tools (JD parse, outreach, interview Qs, compare).
- 🟩 **Deterministic nodes** are everything with a single correct answer — the
  pre-flight shortcuts, all the gates (`simple?`, `cache?`, `confidence`, `empty?`),
  retrieval/RRF/cross-encoder/scoring/MMR, SQL, and the recovery + chips.
- The agent is the **only** router; the few-shot examples bias it. Nothing hard-blocks
  a tool, so multi-intent messages still work.
