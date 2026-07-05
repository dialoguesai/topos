"""§F.2 Canary Extraction Rate — tier-1 CI gate.

Plant one secret per sensitivity class, run the grantee query battery, and count canaries
recoverable from any response. Target 0 for every blocked class. The owner path is run too
and MUST recover the canaries — otherwise a broken corpus would report a vacuous zero.
"""
