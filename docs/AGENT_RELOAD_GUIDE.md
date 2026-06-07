# Agent Reload Guide

## Understanding the Issue

**Problem**: Changes to agents (adding/removing agent files) only take effect when the server is completely restarted, not with `--reload` flag alone.

**Root Cause**: 
1. Agent discovery happens at module import time
2. Python bytecode cache (`.pyc` files) persists even after source files are deleted
3. FastAPI routes are registered at startup and cannot be easily unregistered

## Solutions

### 1. Full Server Restart (Recommended)

This is the most reliable method and works for both adding and removing agents.

```bash
# Stop the server (Ctrl+C if running)
# Then use the helper script:
./start_server.sh

# Or manually:
./clean_cache.sh
uvicorn agent_service:app --host 0.0.0.0 --port 8001 --reload
```

### 2. Reload Endpoint (For Adding New Agents Only)

If you only want to add new agents without restarting:

```bash
./reload_agents.sh

# Or manually:
curl -X POST http://localhost:8001/admin/reload-agents
```

**Limitation**: This can add new agents but cannot remove old routes. Use full restart for removals.

## Common Scenarios

### Adding a New Agent

```bash
# 1. Create your agent file
cat > agents/my_new_agent.py << 'EOF'
from agents.base import AgentSpec
from runtime.schemas import PromptRequest

def run(req: PromptRequest) -> dict:
    return {"input": req.input, "output": "Hello from new agent!"}

AGENT = AgentSpec(
    name="my-new-agent",
    route="/chat/my-new-agent",
    request_model=PromptRequest,
    handler=run
)
EOF

# 2. Reload (either option works)
./reload_agents.sh        # Quick reload (adds only)
# OR
./start_server.sh         # Full restart (recommended)
```

### Removing an Agent

```bash
# 1. Remove the agent file
rm agents/unwanted_agent.py

# 2. MUST do full restart
./start_server.sh
```

### Modifying an Existing Agent

```bash
# Edit the file
vim agents/existing_agent.py

# Uvicorn --reload should pick this up automatically
# If not, do a full restart:
./start_server.sh
```

## Helper Scripts Reference

| Script | Purpose | When to Use |
|--------|---------|-------------|
| `start_server.sh` | Clean cache + start server | Initial start, after add/remove agents |
| `clean_cache.sh` | Clear Python bytecode cache | Before manual server start |
| `reload_agents.sh` | Trigger reload endpoint | Quick add of new agents (no restart) |

## Technical Details

### What Happens on Startup

1. `agent_service.py` imports and creates FastAPI app
2. `@app.on_event("startup")` triggers `register_agents()`
3. `register_agents()` calls `build_registry()`:
   - Scans `agents/` directory for Python modules
   - Imports each module and looks for `AGENT` constant
   - Generates IDs and populates in-memory registry
   - Returns list of AgentSpec objects
4. Routes are registered dynamically for each agent
5. Server prints registered agents and is ready

### Why Deletions Require Full Restart

- FastAPI doesn't provide a clean way to unregister routes
- Once a route is added to `app.routes`, it persists for the app lifetime
- The only way to truly remove routes is to restart the Python process

### Python Cache Gotcha

Even after deleting a source file:
```bash
agents/
  my_agent.py         # DELETED
  __pycache__/
    my_agent.cpython-313.pyc  # STILL EXISTS!
```

Python's import system will load from `.pyc` files, making deleted agents appear to still exist. Always clear cache when removing files.

## Troubleshooting

### Agent still shows up after deletion

```bash
# Clear cache and restart
./start_server.sh
```

### New agent not appearing

```bash
# Check the agent file has AGENT spec
grep "^AGENT = " agents/your_agent.py

# Try reload endpoint
./reload_agents.sh

# If still not working, full restart
./start_server.sh
```

### Import errors on startup

```bash
# Check the agent imports
python -c "from agents.your_agent import AGENT; print(AGENT)"

# If errors, fix the agent file and restart
./start_server.sh
```

### Reload endpoint not working

```bash
# Make sure server is running
curl http://localhost:8001/health

# Check the reload response
curl -X POST http://localhost:8001/admin/reload-agents | python -m json.tool
```

## Best Practices

1. **Always use `start_server.sh` for initial startup** - ensures clean state
2. **Restart after removing agents** - reload endpoint can't unregister routes
3. **Use reload endpoint for quick adds during development** - faster than full restart
4. **Clear cache if unexpected behavior** - when in doubt, `./clean_cache.sh`
5. **Check `/docs` endpoint** - verify your routes are registered at `http://localhost:8001/docs`
