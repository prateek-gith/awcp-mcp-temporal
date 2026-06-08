import socket
import urllib.request
import json
import sys

def check_port(host, port):
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except (socket.timeout, ConnectionRefusedError):
        return False

def check_http_json(url):
    try:
        with urllib.request.urlopen(url, timeout=2.0) as response:
            if response.status == 200:
                return json.loads(response.read().decode('utf-8'))
    except Exception:
        pass
    return None

def main():
    print("================================================================")
    print("🔍 AWCP Service Status & Verification Checker")
    print("================================================================\n")
    
    # List of services to check ports for
    services = [
        {"name": "Temporal Dev Server", "host": "localhost", "port": 7233, "critical": True, "desc": "Orchestrates governance workflows"},
        {"name": "Ollama LLM Server", "host": "localhost", "port": 11434, "critical": True, "desc": "Serves local LLM models"},
        {"name": "FastAPI Control API", "host": "localhost", "port": 8080, "critical": True, "desc": "Exposes HTTP trigger endpoints & serves UI"},
        {"name": "Grafana UI", "host": "localhost", "port": 3000, "critical": False, "desc": "Observability visualization dashboard"},
        {"name": "Prometheus", "host": "localhost", "port": 9090, "critical": False, "desc": "Metrics backend"},
        {"name": "Grafana Loki", "host": "localhost", "port": 3100, "critical": False, "desc": "Logs backend"},
        {"name": "Grafana Tempo", "host": "localhost", "port": 3200, "critical": False, "desc": "Traces backend"},
        {"name": "OTel Collector", "host": "localhost", "port": 4318, "critical": False, "desc": "Receives app telemetry signals"},
    ]
    
    all_ok = True
    
    # 1. Port Verification
    print("📡 1. Checking Service Listening Ports:")
    for svc in services:
        status = check_port(svc["host"], svc["port"])
        status_str = "🟢 RUNNING" if status else ("🔴 DOWN" if svc["critical"] else "🟡 DOWN (Optional/Observability)")
        print(f"  - {svc['name']:<25} ({svc['host']}:{svc['port']}): {status_str}")
        if not status and svc["critical"]:
            all_ok = False
            
    print()
    
    # 2. Ollama Model Verification
    if check_port("localhost", 11434):
        print("🤖 2. Checking Ollama Local Models:")
        tags = check_http_json("http://localhost:11434/api/tags")
        if tags and "models" in tags:
            loaded_models = [m["name"].split(":")[0] for m in tags["models"]]
            required_models = ["gemma2", "llama3.1"]
            for model in required_models:
                found = False
                for m in tags["models"]:
                    if m["name"].startswith(model):
                        found = True
                        print(f"  - Model '{m['name']}' is pulled and available. (OK)")
                        break
                if not found:
                    print(f"  - ⚠️ Model '{model}' NOT found! Please run: ollama pull {model}")
                    all_ok = False
        else:
            print("  - ⚠️ Could not fetch models from Ollama API.")
            all_ok = False
    else:
        print("🤖 2. Ollama is down. Skipping model checks.")
        all_ok = False
        
    print()
    
    # 3. Control API Registration Check
    if check_port("localhost", 8080):
        print("📋 3. Verifying Control API & Agent Registry:")
        agents = check_http_json("http://localhost:8080/agents")
        if isinstance(agents, list):
            print(f"  - API is healthy. {len(agents)} agents registered:")
            for a in agents:
                print(f"    * {a['name']} ({a['runtime']} - {a['status']})")
        else:
            print("  - ⚠️ API response invalid. Make sure worker is connected.")
            all_ok = False
    else:
        print("📋 3. Control API is down. Skipping registry checks.")
        all_ok = False
        
    print("\n================================================================")
    if all_ok:
        print("✅ SUCCESS: All critical services and models are ready!")
        print("You are good to proceed with testing & verification.")
    else:
        print("❌ ATTENTION: Some critical services or models are missing.")
        print("Please check the DOWN/missing services above and start them.")
    print("================================================================")

if __name__ == "__main__":
    main()
