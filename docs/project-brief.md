# Product direction update — 2026-09-24

The intended user experience is a guided clarification process. An initial investigation
identifies potential blind spots and missing information; the user answers manageable
questions before receiving a short conclusion tailored to their circumstances. This
addresses the reading and memory burden of presenting every expert question at once.

Planned interaction:

1. Investigate the original plan from independent perspectives and consolidate overlapping
   concerns and questions.
2. Present one focused question at a time, with its originating perspective(s) and a brief
   explanation of why the answer matters. Offer suggested answers where useful and free text.
3. Support answering now, explicitly marking a question not applicable, saying “I don't
   know,” or saving it for later.
4. Reassess using the original plan and accumulated answers after a small batch. Prefer
   questions whose answers could materially change the conclusion; avoid asking every
   generated question or repeating questions already answered.
5. Return a concise account of remaining concerns, concerns addressed by the answers, and
   unresolved uncertainties. Allow the user to finish early with the available information.

Answers are user-reported context, not independently verified evidence. Apply each answer
only to the concerns it actually addresses: vaccination information does not resolve
introduction timing, and a carer's experience does not establish their availability.
“I don't know” preserves uncertainty; deferral leaves a question open; “not applicable”
records the user's assessment rather than silently proving every related concern resolved.
Do not interpret missing information as an absent arrangement. Keep original findings and
the relationship between questions, answers, and revised conclusions inspectable.

This is agreed product direction, **not implemented behavior or a finalized architecture**.
The current CLI runs team selection, parallel experts, and a single synthesis without user
clarification. Keep it as a baseline while developing this flow incrementally. The next
design step is a minimal question/answer state contract followed by one testable
clarification round. Exact schemas, suggested-answer generation, batch size, stopping
rules and call budgets, reassessment responsibilities, and persistence remain open. Do not
assume every answer requires rerunning every expert. No UI, database, agent framework, or
automatic multi-round orchestration is required by this direction update alone.

The original brief below preserves the initial exploration. Its architecture diagrams,
limits, and statements about implementation status are historical proposals; consult
`AGENTS.md` for current implementation scope and this update for the clarification direction.

# Original project brief

I want to build a pet project called Blindspot.
The primary purpose of the project is to learn and experiment with:
- multi-agent systems;
- agent pipelines and orchestration;
- dynamic agent selection;
- skills/tools;
- structured outputs;
- shared state;
- agent-to-agent review;
- adaptive pipelines;
- prompt design;
- model routing;
- evaluation of agent pipelines;
- Python backend development.
I am an experienced Senior JavaScript/TypeScript engineer, but I am currently learning Python, so I specifically want the backend of this project to be written in Python.
This should be a real, useful application rather than an artificial technical demo.
Blindspot answers one fundamental question:
"What am I missing?"
A user describes a plan, decision, intention, or idea.
Examples:
- "I want to open a small coffee shop in Warsaw."
- "I'm considering moving to Spain while continuing to work with my existing clients."
- "We want to adopt a second cat."
- "I'm planning to work remotely from Thailand for a month."
- "I want to buy an apartment."
- "I'm thinking about leaving my job and building my own product."
Blindspot analyzes the situation from multiple independent perspectives and tries to discover:
- hidden assumptions;
- things the user has forgotten to consider;
- dependencies;
- risks;
- second-order effects;
- missing information;
- contradictions;
- timing problems;
- human factors;
- unexpected consequences;
- questions the user does not yet realize they should be asking.
The objective is NOT to generate an enormous pessimistic list of everything that could theoretically go wrong.
The objective is to find a relatively small number of useful, non-obvious, actionable blind spots.
The multi-agent architecture should be essential to the product.
I don't want:
```text
User
  ↓
Huge prompt
  ↓
LLM
  ↓
Answer
```
with several fake "agents" added only for demonstration.
I want to experiment with situations where independent agents, different perspectives, cross-review, dynamic routing, and iterative investigation actually improve the result.
The system should also be observable so I can inspect why the pipeline behaved the way it did.
Eventually I want to see traces approximately like:
```text
User request
    ↓
Context Analyzer
    ↓
Orchestrator
    │
    ├── Financial Expert
    ├── Legal Expert
    ├── Operations Expert
    └── Custom Expert
            ↓
       Expert Reports
            ↓
       Cross-review
            ↓
   Contradiction Detection
            ↓
      Devil's Advocate
            ↓
         Premortem
            ↓
        Synthesizer
```
The exact architecture is NOT finalized. I want to design and experiment with it rather than blindly implement this diagram.
One of the central ideas is a hybrid dynamic expert system.
I do NOT want a separate hardcoded Python implementation such as:
```python
FinancialAgent()
LegalAgent()
TravelAgent()
CatAgent()
CoffeeShopAgent()
```
Instead, there should be a generic Expert implementation whose runtime configuration determines what kind of expert it becomes.
Conceptually:
```python
ExpertAgent(definition)
```
where definition contains things such as:
- role/perspective;
- objective;
- questions to investigate;
- available skills;
- possibly other configuration.
The exact schema still needs to be designed.
The Orchestrator should have access to a set of broad predefined perspectives.
Initial rough list:
- Financial
- Legal & Regulatory
- Logistics
- Operations
- Timeline & Dependencies
- Human & Behavioral
- Health & Safety
- Security & Privacy
- Technical
- Social & Relationships
- Environmental
- Travel & Mobility
These are NOT final.
The Orchestrator selects only perspectives relevant to the particular situation.
However, it can also dynamically create a custom expert when an important perspective isn't adequately represented by the predefined set.
Example:
```text
User:
"I want to open a cafe where customers can bring their cats."

Selected predefined perspectives:
- Financial
- Legal & Regulatory
- Operations

Dynamically created expert:
- Animal Welfare & Multi-Cat Environment Expert
```
The goal is to combine predictability with the ability to discover unusual domains.
The Orchestrator should try to create the:
smallest useful expert team that provides sufficiently broad coverage
We explicitly want to avoid:
```text
Financial Expert
Budget Expert
Cost Expert
Personal Finance Expert
Long-Term Finance Expert
```
all investigating essentially the same thing.
Rough initial constraint:
```text
Typical: 3–5 experts
Initial maximum: 7
```
But complexity should influence the number of experts.
An important future experiment is allowing the pipeline to change during execution.
For example:
```text
Initial investigation
        ↓
Legal Expert discovers an unexpected issue
        ↓
Orchestrator evaluates the new information
        ↓
New specialized expert is spawned
        ↓
Second investigation round
```
So the graph does not necessarily need to be completely known before execution starts.
This is one of the areas I particularly want to explore.
Roles can be dynamic.
Capabilities should be controlled by the application.
Conceptually there will eventually be a skill registry:
```python
skill_registry = {
    "web_search": ...,
    "calculator": ...,
    "maps": ...,
    "document_reader": ...,
    ...
}
```
An agent may request or be assigned particular skills, but the application decides what actually exists and what the agent is allowed to use.
Principle:
LLMs may plan. The application controls execution and permissions.
We have NOT designed the skill system yet.
These are ideas, not finalized requirements.
Experts initially investigate independently to reduce groupthink.
Experts can inspect findings produced by other experts and:
- support them;
- challenge them;
- expand them;
- identify assumptions;
- mark them irrelevant.
A separate stage can detect cases where agents made incompatible assumptions.
Example:
```text
Agent A assumed the user will rent the property.
Agent B assumed the user will purchase it.
```
A dedicated agent receives the investigation so far and asks:
"What did everyone else miss?"
It should particularly look for:
- hidden assumptions;
- second-order effects;
- cross-domain consequences;
- rare but high-impact issues;
- human factors;
- dependencies.
The system imagines:
"Six months later, this plan failed. What plausible paths led to the failure?"
The resulting failure paths can be compared with existing findings to identify additional blind spots.
The final agent should ideally NOT conduct a completely new investigation.
Its responsibility is to turn validated findings into a concise, useful result.
The final UX should favor something like:
```text
You may not have considered these 5 things
```
rather than dumping 40 generic risks on the user.
A potentially important product feature is an explicit graph of assumptions and dependencies.
For example:
```text
[Open business in 6 months]
          |
          ├── assumes → [Find premises in 2 months]
          │                    |
          │                    └── depends on → [Budget]
          │
          ├── assumes → [Permit process < 3 months]
          │
          └── assumes → [Equipment arrives on time]
```
One of the deeper ideas behind Blindspot is:
People often don't know which assumptions they are treating as facts.
I want the system to help expose those assumptions.
I want to deliberately experiment with using different models for different responsibilities.
My current rough idea is:
- Codex / Astra — help me design the system, generate and critique prompts, reason about pipeline architecture, evaluate outputs, and act as a higher-quality reviewer.
- A cheaper Claude model — execute many of the runtime agent tasks where high-end reasoning may not be necessary.
So the project may intentionally become a multi-model "Frankenstein".
This is desirable: I want to learn where model quality matters and where a cheaper model performs sufficiently well.
Model routing should therefore eventually be a first-class architectural concern rather than every agent automatically using the same model.
Do not assume the current model allocation is optimal. I want to test it empirically.
I don't only want to build the pipeline. I want to be able to compare pipeline designs.
Examples:
```text
A:
Single strong model

B:
Orchestrator
→ parallel experts
→ synthesis

C:
Orchestrator
→ experts
→ cross-review
→ Devil's Advocate
→ synthesis

D:
Adaptive pipeline with second investigation round
```
Potential metrics include:
- number of genuinely useful blind spots;
- unique findings;
- duplicate findings;
- false positives;
- generic/obvious findings;
- relevance;
- actionability;
- token usage;
- latency;
- cost;
- effect of cross-review;
- effect of the second investigation round;
- effect of dynamic vs predefined experts;
- performance difference between models.
The evaluation approach itself still needs to be designed.
Current preference:
- Python backend;
- probably FastAPI;
- Pydantic / structured outputs;
- async where appropriate;
- persistent pipeline state eventually;
- frontend can come later;
- agent framework is NOT selected yet.
Do not assume LangGraph or another agent framework is automatically the right choice.
I want to understand the mechanics first and make an intentional framework decision.
Since one of my goals is learning Python, please favor clear Python architecture and explain Python-specific patterns when they are useful, rather than hiding everything behind a framework.
We are still at the architecture/design stage.
No detailed expert role prompts have been finalized yet.
We intentionally postponed writing detailed prompts for:
- Financial Expert;
- Legal Expert;
- Devil's Advocate;
- Synthesizer;
- etc.
The next architectural question we were considering was the contract between an Expert and the rest of the pipeline — particularly the structure of an expert Finding.
A rough, NON-FINAL idea was:
```python
Finding(
    title="No budget contingency",
    description="...",
    category="financial",
    severity=0.8,
    confidence=0.92,
    evidence=[...],
    assumptions=[...],
    suggested_action="..."
)
```
We need to challenge and properly design this rather than simply adopting it.
Act as a technical partner and critical reviewer while I build Blindspot.
Do not simply agree with my architecture.
When appropriate:
- challenge unnecessary complexity;
- identify places where I'm using agents without a good reason;
- point out when a deterministic Python function would be better than another LLM call;
- point out where agent independence matters;
- distinguish orchestration from role-playing;
- suggest experiments rather than treating uncertain architectural choices as facts;
- think about observability and evaluation from the beginning;
- consider cost and latency;
- prevent the system from turning into an uncontrolled swarm of agents.
At the same time, this is a pet project for learning.
Interesting architecture and experimentation have value even when the absolute simplest production implementation would be smaller.
So optimize for:
useful product + learning + meaningful agent experimentation
rather than pure production minimalism.
Do NOT start implementing the entire application.
First help me design the core domain contracts and the first minimal pipeline.
In particular, I want to decide:
1. What should the shared pipeline state contain?
2. What exactly is an ExpertDefinition?
3. What exactly should an expert return?
4. What is a Finding, and which fields actually belong there?
5. How should evidence, assumptions, confidence and severity be represented?
6. What information should remain deterministic application state versus LLM-generated state?
7. What is the smallest pipeline we can build that is already useful enough to evaluate?
After we agree on those contracts, we can design the prompts for individual roles and only then start implementing the first Python version.
Please preserve this context as we continue designing Blindspot.
