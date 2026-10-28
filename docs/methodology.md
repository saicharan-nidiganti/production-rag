# Methodology

## Goal

The goal of this project is to evaluate and build a RAG system using measurable engineering criteria rather than relying only on subjective demonstrations.

## Core Principles

### 1. Reproducibility

Experiments should be repeatable using documented configurations and datasets.

### 2. Evaluation First

Important implementation decisions should be supported by measurable evaluation.

### 3. Grounded Generation

Generated answers should be supported by retrieved evidence.

### 4. Citation Correctness

Citations should correspond to evidence that actually supports the generated claims.

### 5. Regression Prevention

Changes should be evaluated against a stable dataset whenever practical.

### 6. Controlled Complexity

Additional components should only be introduced when they provide measurable value.

## Evaluation Areas

Initial numeric targets for these areas are defined in
[requirements.md — Success criteria](requirements.md#7-success-criteria).

### Retrieval

Potential measurements include:

- Recall
- Precision
- Hit rate
- Ranking quality
- Context relevance

### Generation

Potential measurements include:

- Answer correctness
- Groundedness
- Faithfulness
- Unsupported claims
- Citation coverage

### System Performance

Potential measurements include:

- End-to-end latency
- Retrieval latency
- Generation latency
- Throughput
- Resource utilization

## Golden Dataset

A curated evaluation dataset will be created containing representative questions and expected evidence.

The dataset should be version controlled where appropriate.

## Experimental Discipline

Experiments should document:

- Configuration
- Dataset version
- Model
- Retrieval configuration
- Evaluation methodology
- Results
- Interpretation

Raw experimental evidence should be preserved separately from derived reports.

## Implementation

[evaluation.md](evaluation.md) describes how these principles are applied: the
golden dataset, the metrics, the reports and the regression gate.
