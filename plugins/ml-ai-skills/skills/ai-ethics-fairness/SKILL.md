---
name: ai-ethics-fairness
display_name: AI Ethics & Fairness
description: >
  Use when the user wants to reason about or measure bias and fairness in a
  dataset or model. Trigger phrases: "check my model for bias", "is this
  model fair across groups", "compute demographic parity", "run a fairness
  audit", "disparate impact analysis", "which fairness metric should I use".
  NOT for explaining individual predictions (see explainability) or
  detecting/anonymizing PII (see data-privacy).
type: reference
domain: responsible-ai
level: intermediate
lifecycle: stable
risk_level: medium
evidence_level: primary
last_verified: 2026-10-27
capabilities:
  - demographic-parity
  - equalized-odds
  - disparate-impact-ratio
  - fairness-metric-tradeoffs
  - bias-mitigation-strategies
requires:
conflicts:
related:
  - explainability
  - data-privacy
  - model-evaluation
  - ai-ml-security
inputs: A trained model's predictions plus group/protected-attribute labels for the population being evaluated.
outputs: A set of group-fairness metric values, an explanation of their mutual tradeoffs, and a recommended mitigation approach.
---

## Overview
Covers how bias enters ML systems and how to quantify fairness once a model
exists — which group-fairness metrics to compute, how they trade off against
each other, and the standard mitigation strategies (pre-, in-, and
post-processing). The user walks away knowing which metric answers their
actual question and why no single metric is a silver bullet.

## Key Concepts
- **Where bias comes from.** *Historical/label bias* — the training labels
  encode past discriminatory decisions (e.g. past loan approvals). *Sampling
  bias* — some groups are under-represented in the collected data.
  *Measurement bias* — features or labels are measured with different
  fidelity or meaning across groups (e.g. using arrests as a proxy label for
  offending, when policing intensity differs by neighborhood). Downstream
  fixes can equalize a chosen metric, but if the labels themselves are
  biased, that metric is computed against biased ground truth — the upstream
  problem is masked, not removed.
- **Group fairness metrics** — the main ones, and the question each answers:
  - *Demographic (statistical) parity*: \(P(\hat{y}=1 \mid A=a)\) equal across
    groups \(a\). "Does the model select each group at the same rate?"
  - *Equalized odds*: TPR and FPR equal across groups. "Is the model equally
    accurate for each group, conditioned on the true label?"
  - *Equal opportunity*: TPR equal across groups only (relaxation of
    equalized odds) — common in loan/hiring contexts where false negatives
    (denying a qualified candidate) matter most.
  - *Predictive parity*: precision equal across groups — "when the model says
    yes, is it equally trustworthy for every group?" (For scores, the
    related criterion is *calibration within groups*.)
  - *Disparate impact ratio* ("four-fifths / 80% rule"): each group's
    selection rate divided by the selection rate of the *most-selected*
    group, \(\frac{P(\hat{y}=1|A=a)}{\max_b P(\hat{y}=1|A=b)}\). Under the US
    Uniform Guidelines on Employee Selection Procedures
    ([29 CFR 1607.4(D)](https://www.law.cornell.edu/cfr/text/29/1607.4)),
    a ratio below 0.8 is "generally regarded" as evidence of adverse impact
    in employment selection. It is a rule of thumb, not a bright line — the
    same rule says smaller gaps can still count if statistically and
    practically significant, and larger gaps on small numbers may not — and
    other jurisdictions and domains use different standards.
- **Individual vs. group fairness.** Group fairness balances aggregate rates
  across protected groups; individual fairness demands "similar individuals
  get similar predictions." The two can conflict — satisfying one does not
  guarantee the other.
- **The impossibility results.** Chouldechova (2017) and Kleinberg,
  Mullainathan & Raghavan (2016) showed that when base rates differ across
  groups, a score or classifier that is calibrated / satisfies predictive
  parity **cannot also equalize error rates** (FPR and FNR) across groups
  except in degenerate cases such as perfect prediction. More generally,
  demographic parity (independence), equalized odds (separation), and
  sufficiency (calibration within groups — for a binary classifier, equal
  PPV *and* equal NPV) are *pairwise* incompatible under unequal base rates
  except in degenerate cases (e.g. a predictor independent of the label) —
  see the "Classification" chapter of Barocas, Hardt & Narayanan.
  Precision-only predictive parity is weaker than sufficiency: it conflicts
  with error-rate balance (Chouldechova's identity) but can coexist with
  demographic parity alone. Picking a fairness metric is a value judgment about
  which error the deployment context cares about most, not a purely
  technical choice.
- **Mitigation strategies, by stage:**
  - *Pre-processing* — reweight or resample training examples so groups have
    balanced influence (e.g. Kamiran & Calders reweighing).
  - *In-processing* — add a fairness constraint or regularizer directly to
    the training objective (e.g. Fairlearn's `ExponentiatedGradient`).
  - *Post-processing* — adjust decision thresholds per group after training
    (e.g. Hardt et al.'s equalized-odds post-processing, which may need
    *randomized* thresholds to hit the constraint exactly; Fairlearn's
    `ThresholdOptimizer`). Cheapest to apply but requires access to the
    protected attribute at inference time, which is not always legal or
    available — whether you may collect or use it depends on jurisdiction
    and domain, so check with legal/compliance before choosing a stage.

## Gotchas
- **Metrics conflict by design.** Don't chase "fix demographic parity AND
  equalized odds" — per the impossibility results, that's only possible if
  base rates are equal across groups or the classifier is uninformative
  about the label. Pick the metric that matches the real-world cost of the
  error you're trying to avoid.
- **Removing the protected attribute isn't enough.** Correlated proxy
  features (zip code, name, school) let the model reconstruct group
  membership indirectly ("fairness through unawareness" fails). You typically
  need the attribute *during auditing/mitigation*, just not as a model input.
- **Small subgroup counts produce noisy metrics.** A fairness gap computed on
  40 samples of a minority group can be sampling noise, not a real effect —
  always report confidence intervals or minimum group sizes alongside the
  metric.
- **Aggregation can hide or fabricate disparities (Simpson's paradox).**
  A model can look fair overall while being unfair within every subgroup
  once you condition on a hidden confounder (e.g. department, region) —
  slice by intersections of relevant attributes, not just top-level groups
  (intersectional slices get small fast, so the small-subgroup caveat above
  applies doubly).
- **Accuracy–fairness tradeoffs are real but not fixed.** Constrained
  training often costs some aggregate accuracy; report both numbers rather
  than presenting a "fixed" model as free of tradeoffs.

## References
- [Fairlearn documentation](https://fairlearn.org/) — open-source toolkit with implementations of the metrics and mitigations above.
- [Hardt, Price & Srebro (2016), "Equality of Opportunity in Supervised Learning"](https://arxiv.org/abs/1610.02413) — defines equalized odds/opportunity and the post-processing fix.
- [Barocas, Hardt & Narayanan, *Fairness and Machine Learning*](https://fairmlbook.org/) — the standard free textbook covering the impossibility results and mitigation taxonomy in depth.
- [AIF360 (AI Fairness 360) toolkit](https://github.com/Trusted-AI/AIF360) — reference implementations of bias metrics and pre/in/post-processing mitigation algorithms.
