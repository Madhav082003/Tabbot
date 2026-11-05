"""Write a plain-language explanation of a Tableau workbook - without an LLM.

Built from the parser kit's rich model (structure), the local data engine (numbers) and rule-based checks.
Every number in the text is computed by code on this machine; nothing here calls Claude.
Entry point: tabbot.explain.report.explain_workbook(lw) -> Report.
"""
