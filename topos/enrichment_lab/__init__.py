"""Enrichment Lab: dry-run enrichments on synthetic bundles or real node data.

Mirrors the Filter Lab architecture (bundles / store / service / worker) for
enrichment jobs. Runs are read-only with respect to node data: the lab calls
each job's ``enrich()`` in memory and stores outputs only in lab tables.
"""
