"""Single source of truth for enumerated frontmatter values across the skill library.

Every script (validate_skills.py, router.py, build_dependency_graph.py,
check_freshness.py, generate_index.py) imports from here instead of
hardcoding its own copy of these lists. If you add a new domain or
lifecycle state, change it here ONLY.
"""

TYPE = ("workflow", "reference")

# "security" was added to support Phase 13 (dedicated AI/ML security skills)
# that are distinct from responsible-ai (fairness/privacy policy concerns).
DOMAIN = (
    "foundations",
    "classical-ml",
    "deep-learning",
    "llm",
    "specialized",
    "mlops",
    "responsible-ai",
    "security",
)

LEVEL = ("beginner", "intermediate", "advanced")

# stable    = actively maintained, safe to route to by default
# draft     = written but not yet reviewed for evidence/freshness to the
#             same bar as stable skills; router may still return it but
#             validate_skills.py will not fail CI on lighter evidence bars
# deprecated = kept for backward compatibility; router should prefer
#              whatever `supersedes` it points to
LIFECYCLE = ("stable", "draft", "deprecated")

# Risk of harm if the skill's guidance/code is followed without additional
# review — NOT a measure of content quality. E.g. "how to deploy a model to
# production" is high-risk even if perfectly written, because a mistake can
# cause an outage; "explain attention" is low-risk because it produces no
# side effects.
RISK_LEVEL = ("low", "medium", "high", "critical")

# The dominant evidence tier backing this skill's substantive claims.
# primary                = original research paper(s) / standards text
# official-documentation = vendor/framework docs are the primary source
# established-practice   = widely taught / consensus practitioner knowledge,
#                          not tied to one paper or doc page
# heuristic              = a rule of thumb that is useful but has known
#                          exceptions (must be phrased conditionally in-body)
# opinion                = a preference/style call with no objective backing
EVIDENCE_LEVEL = ("primary", "official-documentation", "established-practice", "heuristic", "opinion")

# Frontmatter keys every skill MUST have, in addition to the pre-existing
# name/display_name/description/type/domain/level/related contract.
REQUIRED_KEYS = (
    "name",
    "display_name",
    "description",
    "type",
    "domain",
    "level",
    "lifecycle",
    "risk_level",
    "evidence_level",
    "last_verified",
    "capabilities",
    "requires",
    "conflicts",
    "related",
    "inputs",
    "outputs",
)

# Optional: only meaningful for skills whose Workflow embeds code against a
# specific package/API surface. A list of free-text strings, e.g.
#   - "scikit-learn: ColumnTransformer/OneHotEncoder(handle_unknown) API stable since 0.20+"
# Every entry MUST make clear whether it was live-verified this session
# (in practice: it wasn't — this repo has no network/package-install access
# during authoring, so these are model-knowledge estimates, not verified
# facts) per docs/FRESHNESS.md.
OPTIONAL_KEYS = ("version_constraints",)

LIST_KEYS = ("capabilities", "requires", "conflicts", "related", "version_constraints")

DATE_KEY = "last_verified"
