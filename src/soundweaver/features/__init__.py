"""One package per output the app produces.

Each feature keeps its pure logic in `service.py` (no Qt) and its settings
panel in `panel.py`.  Features talk to each other only through
`core.project.PlanContext`, never by importing one another.
"""
