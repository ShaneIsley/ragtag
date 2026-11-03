# System card: Policy Desk

What Policy Desk is for, what it's built from, how well it works, and where it falls short. It's written for someone deciding whether to rely on it, or whether a system like it could be approved. The design is in [`design.md`](design.md), the measurements in [`evaluation.md`](evaluation.md), and the live results on the site's status page (`/status`).

| | |
|---|---|
| Version | Before v1.0, as deployed on 2 November 2026 |
| Operator | One person, who builds, deploys and monitors it |
| Status | An independent demonstration, not a UNSW service; its answers aren't UNSW advice |
| Ends | The end of November 2026, when everything is deleted |

## What it's for

Policy Desk answers questions from UNSW staff, students and researchers about how AI may be used at UNSW: which tools are approved, what data may go into them, how new tools are approved, AI in assessment, referencing AI, misconduct, and assurance. It answers from 17 of UNSW's public policies and guidance pages, cites the passage behind every statement, and says so when the pages don't cover a question.

**It isn't for:**
- questions about anything other than AI at UNSW, which it declines
- general knowledge, or sources other than UNSW's
- decisions about a person, such as whether a student cheated; it answers about rules, and decides nothing
- legal advice
- personal information: no question needs it, and the page asks people to leave it out

## What it's built from

| Part | What | Settings |
|---|---|---|
| Answer model | Azure OpenAI `gpt-5-mini`, version 2025-08-07, a GlobalStandard deployment | Low reasoning, at most 2,000 output tokens, prompt `answer.v4`. Upgraded only when the version retires |
| Search rewrite | The same model | Minimal reasoning, prompt `rewrite.v2`: turns a specific question into the general one it's a case of |
| Embeddings | Azure OpenAI `text-embedding-3-small`, in Australia East | 1,536 dimensions, for passages and questions |
| Search | Azure AI Search, hybrid keyword and vector, Free tier | 12 passages per question, at most 4 from any one source |
| Failover model | Claude Haiku 4.5 on Amazon Bedrock, through the AU inference profile | The same prompt. Answers when Azure OpenAI fails, and when a person asks to "Try the other model" |
| Content safety | Azure OpenAI's content filter, with Prompt Shields | Rates every question and answer; a blocked one is declined, and the page shows a question's rating |
| Masking | Microsoft Presidio, with spaCy's small English model | Removes names, contact details and identifiers from every question before it goes anywhere; contact details and identifiers from the documents, keeping the sign-in address formats the pages give |
| Evaluation judges | The same `gpt-5-mini` with the `facts-check.v1` prompt; the Azure AI Evaluation SDK's groundedness and relevance graders | Used only to measure answers, never to write them |

### The data

All 17 sources are UNSW's public pages: 5 from the Teaching Gateway, 4 from UNSW IT (the AI Assurance Framework among them), 4 UNSW policies (information governance, responsible research, the Code of Conduct and the Plagiarism Policy), 3 for students, and 1 Legal & Compliance fact sheet. Each is classified Public, and used under UNSW's site notice or fair dealing: short passages, always with a link. The weekly ingest turns them into about 380 passages of about 200 tokens, each keeping its heading path. Contact details and identifiers in them are masked. Unchanged passages are kept; changed ones are re-embedded; the record of what changed goes to the drift checks.

Nothing a person types trains or changes any model. No question or answer is stored.

## How it answers

1. Names, contact details and identifiers are removed from the question.
2. The rewrite turns it into the broader question it's a case of, and one hybrid search looks for both.
3. If no passage is similar enough to the question itself, it declines without calling the answer model.
4. The answer model writes from the 12 passages only: the direct answer first, then the details, each statement cited, and whether a point is a rule or guidance.
5. An answer without a valid citation is replaced by a decline.
6. The page shows the answer, its sources, which model wrote it, and a trace ID.

## How well it works

The evaluation gate asks 40 questions with known right answers, taken from the documents: 22 it should answer, 7 it should decline, 4 holding made-up personal details, and 7 trying to make it break its rules. Six more test conversations wait for conversation memory. It runs before every release, and against the live service nightly (13 questions) and weekly (all 40).

The latest runs, on 2 November 2026:

| Measure | What it is | Minimum | Release run 15 | Live, 10:54 am | Live, 11:53 am |
|---|---|---|---|---|---|
| Facts | Share of the expected facts the answer states, judged by meaning | 0.68 | 0.822 | 0.856 | 0.839 |
| Groundedness | Claims supported by the passages cited, out of 5 | 0.95 | 1.000 | 1.000 | 0.987 |
| Relevance | The answer addresses the question, out of 5 | 0.97 | 0.991 | 0.991 | 1.000 |
| Recall | Expected sources found by the search | 0.87 | 0.933 | 0.933 | 0.922 |
| Cited | Answers citing an expected source | 0.94 | 0.967 | 0.967 | 0.967 |
| Latency | 9 in 10 answers within | 9.9 s | 6.7 s | 7.3 s | 6.5 s |

Every rule held in every one of these runs: the right answer or decline, none of what an injection asked for, nothing unsafe, no expected fact contradicted, apart from the known issues below.

**What the numbers mean.** About one expected fact in six is left out, not wrong. One run's facts score moves by several points with nothing changed, because each run draws one answer per question; a targeted comparison measured this, so trends matter more than one run.

**The judges themselves** are checked nightly against 25 fixed answers whose right marks are known. On the first run they agreed with 60 of 61 marks; the miss was the groundedness grader giving full marks to an answer that invented a procedure in the documents' own words. The facts check is the stronger guard against invented rules.

**The failover model** is weaker. In its first weekly check, Claude Haiku 4.5 stated half the expected facts, with groundedness of 0.88: below what the main model must meet. It keeps the service answering when Azure fails, at lower quality.

## Known limitations

**Wrong answers that come and go.** The gate reports these as warnings until a fix is shown to work:

| Question | What goes wrong | How often |
|---|---|---|
| Referencing ChatGPT in APA | Gives the Harvard format, or its own wording, instead of APA's | About a third of full runs |
| Referencing ChatGPT in Harvard | Gives the APA format | Occasionally |
| Using DeepSeek for UNSW work | Merges two sentences, letting UNSW data go into DeepSeek under a research exception; UNSW says "under any circumstances" | Occasionally |
| Unauthorised AI in an assessment | Says "a zero for the assessment" where UNSW says "a mark of zero for the course" | Occasionally |

**Also:**
- **Coverage.** Questions outside the 17 sources are declined, which is correct but unhelpful. Some topics are covered thinly: asked what Scout is, it describes the pilot and never says Scout is available 24/7 through Moodle.
- **Freshness.** Sources are re-read weekly, so an answer can be up to a week behind a changed page.
- **One question at a time.** Follow-up questions aren't understood in context yet.
- **Where questions go.** The answer model may process a question in any country where Microsoft runs it, including outside the OECD. This is recorded as exemption EX-01 and is acceptable only because questions shouldn't hold personal information.
- **Masking can miss things.** Health information isn't removed, and detection can miss details.
- **Speed.** The first question after a quiet spell can take up to about 14 seconds.

## Safeguards

| Risk | Safeguard |
|---|---|
| A made-up answer | Answers only from retrieved passages; declines when nothing relevant is found; every statement cited and checked; the evaluation measures facts and groundedness nightly |
| An answer taken as permission | The prompt says whether a point is a rule or guidance, and never turns a description into a permission |
| Prompt injection | Passages are quotations, not instructions; questions' instructions about format, tone or words to add are ignored; 7 injection cases in every run |
| Personal information | Masked before anything is sent; nothing stored; no IP addresses recorded; the page asks people to leave it out |
| Harmful content | Azure's content filter on every question and answer; the toxicity rule in every run |
| Quality slipping unnoticed | The nightly and weekly runs against the live service, judge calibration, drift checks, alarms by email, and the public status page |
| Over-reliance | "Not UNSW advice" on the page; sources linked so the passage can be checked; the advice to check course outlines |

## Accountability

The operator owns the system, its privacy impact assessment ([`privacy-impact-assessment.md`](privacy-impact-assessment.md)), its exemptions ([`exemptions.md`](exemptions.md)), its design decisions ([`decisions.md`](decisions.md)) and its [runbook](runbook.md). The AI Assurance Framework self-assessment is in [`ai-assurance-self-assessment.md`](ai-assurance-self-assessment.md). Every answer can be traced, step by step, from the trace ID the page shows; every evaluation run is recorded with what it measured and what measured it.
