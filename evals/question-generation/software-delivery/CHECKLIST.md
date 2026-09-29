# Software delivery (fictional): question checklist

Apply the general checks in `../README.md` first.

**Plan (already answered, so not to be asked):** a team of four developers, eight weeks, a
customer portal for a client, the existing authentication service, and the final design due
by the end of week two.

| ID | Perspective | Finding (short) |
| --- | --- | --- |
| `e1-f1` | Technical | The client's users may not exist in the authentication service |
| `e1-f2` | Technical | Acceptance criteria are not described |
| `e2-f1` | Timeline & Dependencies | A late design compresses build time |
| `e2-f2` | Timeline & Dependencies | Team availability across the eight weeks |
| `e3-f1` | Security & Privacy | Personal-data handling requirements |

## Case-specific checks

- [ ] **Five findings, at most three questions.** The questions chosen are those most
      likely to change the analysis (for example user migration, capacity, personal data).
      Leaving `e1-f2` or `e2-f1` unasked is acceptable. Cramming them into other questions is
      not.
- [ ] **No question about team size, the deadline, or when the design is due.** A question
      about what happens *if* the design is late is allowed only if it asks something the
      plan doesn't state, such as whether work can start before the design.
- [ ] **Users and data stay separate.** "Are the client's users already in your
      authentication service?" and "Will the portal store personal data?" can get different
      answers and cite different findings.
- [ ] **Capacity is one question.** Asking whether the four developers are full-time on the
      project is one topic. Adding "and who is the client contact" is not.

## Examples (illustrative, not required wording)

Acceptable:
- "Are the client's end users already registered in your existing authentication service?"
- "Will all four developers work on this project full-time for the eight weeks?"
- "Will the portal store or display customers' personal data?"

Bundled (fail):
- "Are the users in your auth service, and what data will the portal store?"
- "Has the client agreed acceptance criteria and a change-request process?"
- A `why_it_matters` of "…; also let us know whether the client has a security policy"
