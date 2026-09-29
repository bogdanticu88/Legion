import importlib.util

# MCP is an optional extra. Without it the rest of the suite still has to pass, which is what the
# base CI job checks.
collect_ignore_glob = [] if importlib.util.find_spec("mcp") else ["unit/test_mcp*.py", "mcp_lab.py"]
