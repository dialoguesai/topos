"""Privacy-claims eval suite.

Operationalizes PLAN_PRIVACY_FIREWALL_AND_EVALS.md: turns each cognitive-firewall
privacy claim into a measured, gate-able number. Layout mirrors the plan's §A.5:

  common/   shared probe model, canary corpus builder, leak detector, report schema
  uar/      §F.1 Unauthorized Access Rate — tier-1 CI gate (must find zero leaks)

Further modules (cer/, leakage/, minimality/, negotiation/, perf/, dense/) land per
the plan's sequencing.
"""
