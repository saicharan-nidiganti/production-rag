---
name: Evaluation problem
about: A wrong label, a metric that misleads, a flaky gate, or a quality regression
title: "eval: "
labels: area:evaluation
---

<!--
Do not include API keys or private documents. Questions and passages from the
public evaluation corpus (eval/corpus/) are fine to quote.
-->

## What is wrong

<!-- For example: a golden-dataset label, a metric definition, a threshold, or a result. -->

## Case or metric

- Dataset and case id (for example `eval/golden_v2.jsonl`, `t-par-02`):
- Metric or gate (for example `splits.test.retrieval.mrr`, `eval/thresholds.json`):

## Configuration

<!-- Paste the `config` block of the report, or the command you ran. No keys. -->

```text
```

## Evidence

<!--
What you expected and what you got. Attach the relevant part of the JSON report
(eval/reports/...). Say whether the run was offline or used a paid model.
-->

## Suggested fix

<!--
Labels change by adding a new dataset version, not by editing an existing one
(docs/evaluation.md). Thresholds are calibrated on the dev split only.
-->
