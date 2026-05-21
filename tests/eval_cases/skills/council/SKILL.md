---
name: council
trigger: /council
permission: EXECUTE
version: 0.1.0
council: true
args_schema:
  type: object
  properties:
    question:
      type: string
      description: Topic the in-loop council should debate.
    panel:
      type: array
      items:
        type: string
  required: [question]
lineage:
  - llm-council/master
  - hermes/agents/judge
---
Council mode — fan a single question out to multiple models, fuse verdicts.
Returns a single ranked answer with per-panelist dissent if any.
