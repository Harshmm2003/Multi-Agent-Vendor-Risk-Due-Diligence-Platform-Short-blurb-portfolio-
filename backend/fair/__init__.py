"""
FAIR: an agent that quantifies risk in dollars with the FAIR model.

A standalone package. It must never import the VendorIQ application (`app`);
VendorIQ is one host that plugs in through fair/ports.py (step 2). A test
enforces the boundary: tests/fair/test_fair_boundary.py.

    fair.model   the math engine: FAIR taxonomy, PERT, Monte Carlo, Altman Z
    fair.agent   the agentic layer (step 2)

Command line:  python -m fair simulate fair/examples/vendor_breach_healthcare.json
"""
