---
name: data-privacy
display_name: Data Privacy & Anonymization
description: >
  Use when the user wants to protect personal data in a dataset or training
  pipeline. Trigger phrases: "remove PII from this dataset", "anonymize this
  data", "add differential privacy to training", "redact personal
  information", "how many people can I re-identify from this table",
  "k-anonymity for this table", "membership inference attack on my model",
  "is my model memorizing training data", "training data extraction attack".
  NOT for fairness/bias metrics (see ai-ethics-fairness), explaining model
  predictions (see explainability), or prompt injection, data poisoning and
  model supply-chain security (see ai-ml-security).
type: reference
domain: responsible-ai
level: intermediate
lifecycle: stable
risk_level: medium
evidence_level: established-practice
last_verified: 2026-10-27
capabilities:
  - k-anonymity
  - l-diversity
  - t-closeness
  - differential-privacy-budgeting
  - dp-sgd-training
  - quasi-identifier-detection
  - training-data-memorization
  - membership-inference
  - training-data-extraction
requires:
conflicts:
related:
  - ai-ethics-fairness
  - explainability
  - data-preprocessing
  - ai-ml-security
inputs: A dataset or training pipeline containing personal data, plus the intended release or deployment context.
outputs: An inventory of direct/quasi-identifiers and a recommended anonymization or differential-privacy technique matched to the threat model.
---

## Overview
Covers how personal data ends up identifiable even after "cleaning," and the
main technical tools for reducing that risk before or during modeling:
identifying PII and quasi-identifiers, classic anonymization guarantees
(k-anonymity and its extensions), and differential privacy for
statistics/ML training. The user walks away knowing which tool matches which
threat model, and why "we removed the names" is rarely sufficient.

## Key Concepts
- **Direct identifiers vs. quasi-identifiers.** *Direct identifiers* (name,
  email, SSN, phone) obviously identify someone and are easy to strip.
  *Quasi-identifiers* (zip code, birth date, gender, job title) don't
  identify anyone alone but **combine** to make a large share of records
  unique — Sweeney's classic estimate (from 1990 US census data) was that
  ~87% of the US population is likely unique on just {5-digit ZIP, birth
  date, gender}; later re-analysis on 2000 census data (Golle, 2006) put the
  figure lower but still a majority, and the exact rate depends on the
  population and on how finely each field is recorded. Any real privacy
  workflow must inventory quasi-identifiers, not just obvious PII columns.
- **De-identification vs. anonymization vs. pseudonymization.**
  *Pseudonymization* replaces identifiers with a token/key such that records
  can be re-attributed only with additional information kept separately
  (GDPR Art. 4(5)) — the mapping still exists somewhere, so for whoever holds
  or can reasonably obtain it, GDPR still treats the data as "personal data"
  (Recital 26). *Anonymization* aims to be **irreversible**: no reasonable means
  exist to re-link records to individuals — this "reasonable means" bar is
  the EU/GDPR framing (Recital 26); other jurisdictions define the legal
  anonymization/de-identification line differently (e.g. US HIPAA's Safe
  Harbor and Expert Determination methods), so confirm which jurisdiction's
  specific standard actually applies before treating data as anonymized.
  *De-identification* (the NIST/US HIPAA term) sits between the two
  depending on the technique used — always check which guarantee a
  "de-identification" step actually provides.
- **k-anonymity, l-diversity, t-closeness.** *k-anonymity*: every combination
  of quasi-identifiers must be shared by at least \(k\) records (generalize
  zip codes, bucket ages, etc., until this holds). It doesn't protect against
  a *homogeneity attack* if all \(k\) records in a group share the same
  sensitive value (e.g. same diagnosis) — *l-diversity* fixes this by
  requiring \(\ell\) well-represented sensitive values per group.
  *t-closeness* further requires the sensitive-attribute distribution within
  each group to stay within distance \(t\) (Earth Mover's Distance in the
  original paper) of the overall distribution, defending against the
  *skewness* and *similarity* attacks that l-diversity misses. All three
  degrade badly on high-dimensional, sparse data (purchase histories,
  ratings, location traces), where almost every record is unique and
  enforcing them destroys most of the utility.
- **Differential privacy (DP).** A mathematical guarantee that a mechanism
  \(M\)'s output distribution barely changes whether or not any single
  individual's record is included: for all neighboring datasets \(D, D'\) and
  output sets \(S\), \(\Pr[M(D)\in S] \le e^{\varepsilon}\Pr[M(D')\in S] + \delta\)
  (pure \(\varepsilon\)-DP when \(\delta = 0\)). Achieved by adding noise
  calibrated to the query's sensitivity: the Laplace mechanism (scale
  \(\Delta_1/\varepsilon\), L1 sensitivity) gives pure \(\varepsilon\)-DP and
  suits low-dimensional counts/sums; the Gaussian mechanism (scale
  proportional to L2 sensitivity) gives \((\varepsilon, \delta)\)-DP and is
  usually preferred for high-dimensional outputs because L2 sensitivity can be
  much smaller than L1 there. For ML training, **DP-SGD** (Abadi et al., 2016)
  clips per-example gradients and adds Gaussian noise at each step so the
  trained model itself carries an \((\varepsilon, \delta)\) guarantee, not just
  released statistics. The guarantee's strength depends on \(\delta\)
  (conventionally chosen well below \(1/n\), since \(\delta \approx 1/n\)
  would permit leaking a few records outright) and on the stated
  \(\varepsilon\) covering every training step.
- **Privacy budget composition.** Each DP query/training run spends part of a
  finite \(\varepsilon\) budget; running many queries against the same
  dataset compounds the privacy loss (basic or advanced composition
  theorems), which is why production DP systems track and cap cumulative
  \(\varepsilon\) rather than treating each query independently. For
  iterative training, use a privacy accountant (e.g. the moments/RDP
  accountants used by DP-SGD libraries such as Opacus) — it gives a much
  tighter total than naively summing per-step \(\varepsilon\), but the total
  still grows with steps, epochs, and sampling rate.
- **Re-identification (linkage) attacks.** Combining an "anonymized" release
  with an external dataset that shares quasi-identifiers can re-identify
  individuals even when no direct identifier was ever released — the
  canonical cautionary examples are the 2006 AOL search-log release (users
  identified from the content of their own queries cross-referenced with
  public information) and the Netflix Prize dataset (Narayanan & Shmatikov
  linked sparse movie ratings to public IMDb reviews).
- **Memorization in trained models.** A model can itself leak its training
  data: large language models have been shown to regurgitate verbatim
  training sequences, including personal information, under targeted
  prompting (Carlini et al., 2021), and membership-inference attacks test
  whether a given record was in the training set. Scrubbing PII before
  training reduces this risk; DP-SGD bounds it formally; neither "the model
  is just weights" nor a non-public dataset is a defense. NIST AI 100-2
  classifies these as privacy attacks on ML; this card, not
  [[ai-ml-security]], owns them.

## Gotchas
- **Pseudonymization is not anonymization.** Hashing or tokenizing a
  direct identifier is reversible if the mapping exists, and a plain
  (unkeyed) hash of a small ID space — phone numbers, SSNs — can be reversed
  by brute-force enumeration; even an irreversible token still lets records be
  linked across datasets. Under GDPR, pseudonymized data remains personal data
  for anyone who can reasonably re-attribute it — get a legal determination
  before treating a pseudonymized release as out of scope.
- **Removing "obvious" PII columns misses quasi-identifiers.** Dropping name/
  SSN/email while leaving zip + birth date + gender in the clear can still
  leave a large share of rows unique and re-identifiable — always
  threat-model the quasi-identifier combination, not just a PII column
  checklist.
- **k-anonymity alone doesn't protect sensitive attributes.** Without
  l-diversity/t-closeness, a k-anonymous group can still leak a sensitive
  value via homogeneity (all k records share it) or background knowledge
  (attacker already knows the target isn't the one exception in the group).
- **DP noise hurts small subgroups disproportionately.** Noise of a fixed
  scale added to satisfy a global \(\varepsilon\) is a much larger *relative*
  error on a count of 20 than on a count of 20,000, and DP-SGD has been shown
  to reduce accuracy more for underrepresented groups (Bagdasaryan et al.,
  2019) — DP and fairness goals can conflict and should be evaluated
  together (see [[ai-ethics-fairness]]), not assumed compatible.
- **Privacy budget composes across queries/epochs.** Treating each DP query
  or each training epoch as "free" because \(\varepsilon\) looks small
  per-step ignores composition — cumulative \(\varepsilon\) across an entire
  pipeline or multi-epoch training run is what actually bounds the guarantee.

## References
- [NIST SP 800-188, De-Identifying Government Datasets: Techniques and Governance](https://csrc.nist.gov/pubs/sp/800/188/final) — authoritative taxonomy of de-identification/re-identification risk and techniques.
- [Dwork & Roth, *The Algorithmic Foundations of Differential Privacy*](https://www.cis.upenn.edu/~aaroth/Papers/privacybook.pdf) — the standard reference text for DP definitions, mechanisms, and composition theorems.
- [Google Differential Privacy library](https://github.com/google/differential-privacy) — open-source implementation of DP mechanisms (Laplace/Gaussian) for real aggregation queries.
- [Abadi et al., 2016 — Deep Learning with Differential Privacy](https://arxiv.org/abs/1607.00133) — introduces DP-SGD (per-example clipping + Gaussian noise) and the moments accountant.
