import os

# Load environment variables from .env file if it exists
if os.path.exists(".env"):
    with open(".env") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith("#") and "=" in line:
                # Commented out key-value. Let's explicitly remove it from the environment if present
                parts = line[1:].strip().split("=", 1)
                key = parts[0].strip()
                if key in os.environ:
                    del os.environ[key]
            elif "=" in line:
                key, val = line.split("=", 1)
                os.environ[key.strip()] = val.strip().strip('"').strip("'")

import requests
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
import io
import base64
import json
import time
import asyncio
from PIL import Image

# Temporal SDK modules - Import only if available (optional dependency)
try:
    from temporalio.client import Client
    from temporalio.worker import Worker
    TEMPORAL_AVAILABLE = True
except ImportError:
    TEMPORAL_AVAILABLE = False
    Client = None
    Worker = None

# API Provider Setup
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROK_API_KEY = os.getenv("GROK_API_KEY")
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY") or os.getenv("NGC_API_KEY")

if GROQ_API_KEY:
    API_URL = os.getenv("GROQ_API_URL", "https://api.groq.com/openai/v1/chat/completions")
    API_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    API_KEY = GROQ_API_KEY
    PROVIDER_NAME = "Groq"
elif GROK_API_KEY:
    API_URL = os.getenv("GROK_API_URL", "https://api.x.ai/v1/chat/completions")
    API_MODEL = os.getenv("GROK_MODEL", "grok-beta")
    API_KEY = GROK_API_KEY
    PROVIDER_NAME = "xAI Grok"
else:
    API_URL = os.getenv("NVIDIA_API_URL", "https://integrate.api.nvidia.com/v1/chat/completions")
    API_MODEL = os.getenv("NVIDIA_MODEL", "meta/llama-3.3-70b-instruct")
    API_KEY = NVIDIA_API_KEY
    PROVIDER_NAME = "NVIDIA NIM"

app = FastAPI(title=f"{PROVIDER_NAME} Agent API")


class PromptRequest(BaseModel):
    input: str


HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>NVIDIA NIM Agent</title>
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&display=swap" rel="stylesheet">
  <style>
    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }

    body {
      min-height: 100vh;
      font-family: 'Outfit', sans-serif;
      background: linear-gradient(135deg, #0f172a 0%, #1e1b4b 100%);
      color: #f1f5f9;
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: 40px 20px;
    }

    main {
      width: min(920px, 100%);
      margin: 0 auto;
    }

    header {
      text-align: center;
      margin-bottom: 40px;
    }

    h1 {
      font-size: 3rem;
      font-weight: 700;
      background: linear-gradient(to right, #38bdf8, #818cf8);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      margin-bottom: 12px;
    }

    .subtitle {
      color: #94a3b8;
      font-size: 1.1rem;
      font-weight: 300;
    }

    /* Tabs Layout */
    .tabs-container {
      background: rgba(30, 41, 59, 0.7);
      backdrop-filter: blur(12px);
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 16px;
      overflow: hidden;
      box-shadow: 0 20px 40px rgba(0, 0, 0, 0.3);
    }

    .tabs-header {
      display: flex;
      background: rgba(15, 23, 42, 0.8);
      border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    }

    .tab-btn {
      flex: 1;
      padding: 16px;
      background: transparent;
      border: 0;
      color: #94a3b8;
      font-size: 1rem;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.3s ease;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
    }

    .tab-btn:hover {
      color: #f1f5f9;
      background: rgba(255, 255, 255, 0.02);
    }

    .tab-btn.active {
      color: #38bdf8;
      background: rgba(56, 189, 248, 0.08);
      border-bottom: 2px solid #38bdf8;
    }

    .tab-content {
      display: none;
      padding: 32px;
    }

    .tab-content.active {
      display: block;
    }

    /* Form Elements */
    .form-group {
      margin-bottom: 24px;
    }

    label {
      display: block;
      margin-bottom: 10px;
      font-weight: 600;
      color: #cbd5e1;
      font-size: 0.95rem;
    }

    textarea, input[type="text"] {
      width: 100%;
      padding: 16px;
      background: rgba(15, 23, 42, 0.6);
      border: 1px solid rgba(255, 255, 255, 0.1);
      border-radius: 8px;
      color: #f1f5f9;
      font-family: inherit;
      font-size: 1rem;
      outline: none;
      transition: all 0.3s ease;
    }

    textarea:focus, input[type="text"]:focus {
      border-color: #38bdf8;
      box-shadow: 0 0 0 3px rgba(56, 189, 248, 0.15);
    }

    textarea {
      min-height: 120px;
      resize: vertical;
    }

    /* File Upload Area */
    .upload-zone {
      border: 2px dashed rgba(255, 255, 255, 0.2);
      border-radius: 12px;
      padding: 40px 20px;
      text-align: center;
      cursor: pointer;
      transition: all 0.3s ease;
      background: rgba(15, 23, 42, 0.4);
    }

    .upload-zone:hover {
      border-color: #38bdf8;
      background: rgba(56, 189, 248, 0.02);
    }

    .upload-zone svg {
      width: 48px;
      height: 48px;
      color: #94a3b8;
      margin-bottom: 12px;
      transition: all 0.3s ease;
    }

    .upload-zone:hover svg {
      color: #38bdf8;
      transform: translateY(-4px);
    }

    .upload-zone input {
      display: none;
    }

    .preview-container {
      margin-top: 20px;
      display: none;
      justify-content: center;
    }

    .preview-image {
      max-width: 100%;
      max-height: 250px;
      border-radius: 8px;
      border: 1px solid rgba(255, 255, 255, 0.1);
      box-shadow: 0 10px 25px rgba(0,0,0,0.2);
    }

    /* Actions and Buttons */
    .actions {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin-top: 24px;
    }

    button.btn-primary {
      border: 0;
      border-radius: 8px;
      padding: 14px 28px;
      background: linear-gradient(135deg, #38bdf8 0%, #2563eb 100%);
      color: #ffffff;
      font-size: 1rem;
      font-weight: 700;
      cursor: pointer;
      transition: all 0.3s ease;
      box-shadow: 0 4px 12px rgba(37, 99, 235, 0.3);
    }

    button.btn-primary:hover {
      transform: translateY(-2px);
      box-shadow: 0 6px 20px rgba(37, 99, 235, 0.4);
    }

    button.btn-primary:disabled {
      opacity: 0.5;
      cursor: not-allowed;
      transform: none !important;
      box-shadow: none !important;
    }

    .status {
      color: #94a3b8;
      font-size: 0.95rem;
      display: flex;
      align-items: center;
      gap: 8px;
    }

    /* Output Panel */
    .output-section {
      margin-top: 32px;
      border-top: 1px solid rgba(255, 255, 255, 0.08);
      padding-top: 24px;
    }

    .output-header {
      font-weight: 600;
      margin-bottom: 12px;
      color: #cbd5e1;
      font-size: 1.1rem;
    }

    .output-box {
      background: rgba(15, 23, 42, 0.8);
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 8px;
      padding: 20px;
      min-height: 120px;
      line-height: 1.6;
      white-space: pre-wrap;
      font-size: 0.95rem;
      color: #e2e8f0;
    }

    .metadata-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }

    .metadata-card {
      background: rgba(255,255,255,0.02);
      border: 1px solid rgba(255,255,255,0.05);
      border-radius: 6px;
      padding: 10px 14px;
    }

    .metadata-label {
      font-size: 0.8rem;
      color: #64748b;
      margin-bottom: 2px;
    }

    .metadata-value {
      font-size: 0.9rem;
      font-weight: 600;
      color: #38bdf8;
    }
  </style>
</head>
<body>
  <main>
    <header>
      <h1>NVIDIA NIM Agent</h1>
      <p class="subtitle">Type a prompt and send it to an NVIDIA-hosted model.</p>
    </header>

    <div class="tabs-container">
      <div class="tabs-header">
        <button class="tab-btn active" onclick="switchTab('text')">
          <svg width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z"/></svg>
          Text Agent
        </button>
        <button class="tab-btn" onclick="switchTab('vision')">
          <svg width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z"/></svg>
          Vision Service
        </button>
        <button class="tab-btn" onclick="switchTab('temporal')">
          <svg width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
          Temporal Pipeline
        </button>
      </div>

      <!-- Text Tab -->
      <div id="text-tab" class="tab-content active">
        <div class="form-group">
          <label for="prompt">Prompt Input</label>
          <textarea id="prompt" placeholder="Ask the model anything..."></textarea>
        </div>

        <div class="actions">
          <span id="text-status" class="status">Ready</span>
          <button class="btn-primary" id="text-send" onclick="sendTextPrompt()">Send Request</button>
        </div>

        <div class="output-section">
          <div class="output-header">Response Output</div>
          <div id="text-output" class="output-box">Response will appear here.</div>
        </div>
      </div>

      <!-- Vision Tab -->
      <div id="vision-tab" class="tab-content">
        <div class="form-group">
          <label>Image Upload</label>
          <div class="upload-zone" onclick="document.getElementById('file-input').click()" ondragover="event.preventDefault()" ondrop="handleDrop(event)">
            <svg fill="none" stroke="currentColor" stroke-width="1.5" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M12 16.5V9.75m0 0l3 3m-3-3l-3 3M6.75 19.5a4.5 4.5 0 01-1.41-8.775 5.25 5.25 0 0110.233-2.33 3 3 0 013.758 3.848A3.752 3.752 0 0118 19.5H6.75z"/></svg>
            <p id="upload-text">Drag and drop your image here, or <strong>browse files</strong></p>
            <input type="file" id="file-input" accept="image/*" onchange="handleFileSelect(event)">
          </div>
          <div class="preview-container" id="preview-container">
            <img src="" id="image-preview" class="preview-image" alt="Upload Preview">
          </div>
        </div>

        <div class="form-group">
          <label for="vision-prompt">Analysis Prompt</label>
          <input type="text" id="vision-prompt" value="Describe this image in detail." placeholder="Enter prompt instructions for the vision model...">
        </div>

        <div class="actions">
          <span id="vision-status" class="status">Ready</span>
          <button class="btn-primary" id="vision-send" onclick="sendVisionPrompt()" disabled>Analyze Image</button>
        </div>

        <div class="output-section" id="vision-output-section" style="display:none;">
          <div class="output-header">Image Metadata</div>
          <div class="metadata-grid">
            <div class="metadata-card">
              <div class="metadata-label">Filename</div>
              <div class="metadata-value" id="meta-filename">-</div>
            </div>
            <div class="metadata-card">
              <div class="metadata-label">Dimensions</div>
              <div class="metadata-value" id="meta-dimensions">-</div>
            </div>
            <div class="metadata-card">
              <div class="metadata-label">Processed Size</div>
              <div class="metadata-value" id="meta-size">-</div>
            </div>
          </div>
          <div class="output-header">Vision Analysis</div>
          <div id="vision-output" class="output-box">Analysis will appear here.</div>
        </div>
      </div>

      <!-- Temporal Tab -->
      <div id="temporal-tab" class="tab-content">
        <div class="form-group">
          <label>Pipeline Image Upload</label>
          <div class="upload-zone" onclick="document.getElementById('temp-file-input').click()" ondragover="event.preventDefault()" ondrop="handleTempDrop(event)">
            <svg fill="none" stroke="currentColor" stroke-width="1.5" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M12 16.5V9.75m0 0l3 3m-3-3l-3 3M6.75 19.5a4.5 4.5 0 01-1.41-8.775 5.25 5.25 0 0110.233-2.33 3 3 0 013.758 3.848A3.752 3.752 0 0118 19.5H6.75z"/></svg>
            <p id="temp-upload-text">Drag and drop your image here, or <strong>browse files</strong> to run the full Temporal pipeline</p>
            <input type="file" id="temp-file-input" accept="image/*" onchange="handleTempFileSelect(event)">
          </div>
          <div class="preview-container" id="temp-preview-container">
            <img src="" id="temp-image-preview" class="preview-image" alt="Upload Preview">
          </div>
        </div>

        <div class="form-group">
          <label for="temp-prompt">Custom VLM Prompt</label>
          <input type="text" id="temp-prompt" value="What objects are in this image? List them and describe the scene in 2 sentences." placeholder="Instructions for VLM activity...">
        </div>

        <div class="actions">
          <span id="temp-status" class="status">Ready</span>
          <button class="btn-primary" id="temp-send" onclick="sendTemporalPrompt()" disabled>Trigger Traceable Temporal Pipeline</button>
        </div>

        <!-- Live Execution & Dashboard -->
        <div class="output-section" id="temp-output-section" style="display:none;">
          <div class="output-header" style="display: flex; justify-content: space-between; align-items: center;">
            <span>Temporal Orchestration Output</span>
            <a id="temp-web-ui-btn" href="#" target="_blank" style="background: rgba(56, 189, 248, 0.15); color: #38bdf8; border: 1px solid #38bdf8; border-radius: 4px; padding: 4px 10px; font-size: 0.8rem; font-weight: 600; text-decoration: none; display: flex; align-items: center; gap: 4px;">
              <svg width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14"/></svg>
              Open Temporal Web UI
            </a>
          </div>
          
          <div class="metadata-grid" style="margin-top: 12px;">
            <div class="metadata-card">
              <div class="metadata-label">Workflow ID</div>
              <div class="metadata-value" id="temp-workflow-id" style="font-family: monospace; font-size: 0.8rem;">-</div>
            </div>
            <div class="metadata-card">
              <div class="metadata-label">Run ID</div>
              <div class="metadata-value" id="temp-run-id" style="font-family: monospace; font-size: 0.8rem;">-</div>
            </div>
            <div class="metadata-card">
              <div class="metadata-label">Pipeline Status</div>
              <div class="metadata-value" id="temp-pipeline-status" style="color: #4ade80;">Completed</div>
            </div>
          </div>
          
          <div style="margin-bottom: 16px;">
            <div class="metadata-label" style="font-size: 0.85rem; font-weight: 600; margin-bottom: 6px; color: #94a3b8;">1. Image Validation Output:</div>
            <div id="temp-val-output" class="output-box" style="margin-bottom: 12px; min-height: 80px; font-family: monospace; font-size: 0.85rem;">-</div>
          </div>

          <div style="margin-bottom: 16px;">
            <div class="metadata-label" style="font-size: 0.85rem; font-weight: 600; margin-bottom: 6px; color: #94a3b8;">2. Image Preprocessing Output:</div>
            <div id="temp-prep-output" class="output-box" style="margin-bottom: 12px; min-height: 80px; font-family: monospace; font-size: 0.85rem;">-</div>
          </div>

          <div>
            <div class="metadata-label" style="font-size: 0.85rem; font-weight: 600; margin-bottom: 6px; color: #94a3b8;">3. VLM Analysis & Final Output:</div>
            <div id="temp-agent-output" class="output-box" style="min-height: 100px;">-</div>
          </div>
        </div>
      </div>
    </div>
  </main>

  <script>
    let activeFile = null;

    function switchTab(tab) {
      document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
      document.querySelectorAll('.tab-content').forEach(content => content.classList.remove('active'));

      if (tab === 'text') {
        document.querySelector('.tab-btn:nth-child(1)').classList.add('active');
        document.getElementById('text-tab').classList.add('active');
      } else if (tab === 'vision') {
        document.querySelector('.tab-btn:nth-child(2)').classList.add('active');
        document.getElementById('vision-tab').classList.add('active');
      } else {
        document.querySelector('.tab-btn:nth-child(3)').classList.add('active');
        document.getElementById('temporal-tab').classList.add('active');
      }
    }

    let activeTempFile = null;

    function handleTempDrop(e) {
      e.preventDefault();
      if (e.dataTransfer.files && e.dataTransfer.files[0]) {
        processTempFile(e.dataTransfer.files[0]);
      }
    }

    function handleTempFileSelect(e) {
      if (e.target.files && e.target.files[0]) {
        processTempFile(e.target.files[0]);
      }
    }

    function processTempFile(file) {
      if (!file.type.startsWith('image/')) {
        alert('Please drop an image file.');
        return;
      }
      activeTempFile = file;

      const reader = new FileReader();
      reader.onload = function(e) {
        const preview = document.getElementById('temp-image-preview');
        preview.src = e.target.result;
        document.getElementById('temp-preview-container').style.display = 'flex';
        document.getElementById('temp-upload-text').innerHTML = `Selected file: <strong>${file.name}</strong>`;
        document.getElementById('temp-send').disabled = false;
      };
      reader.readAsDataURL(file);
    }

    async function sendTemporalPrompt() {
      if (!activeTempFile) return;

      const sendEl = document.getElementById("temp-send");
      const statusEl = document.getElementById("temp-status");
      const outputSection = document.getElementById("temp-output-section");
      
      const workflowIdEl = document.getElementById("temp-workflow-id");
      const runIdEl = document.getElementById("temp-run-id");
      const pipelineStatusEl = document.getElementById("temp-pipeline-status");
      const valOutputEl = document.getElementById("temp-val-output");
      const prepOutputEl = document.getElementById("temp-prep-output");
      const agentOutputEl = document.getElementById("temp-agent-output");
      const webUiBtn = document.getElementById("temp-web-ui-btn");

      sendEl.disabled = true;
      statusEl.textContent = "Launching Temporal Workflow...";
      outputSection.style.display = 'none';

      const formData = new FormData();
      formData.append("file", activeTempFile);
      formData.append("prompt", document.getElementById("temp-prompt").value.trim());

      try {
        const response = await fetch("/run-traceable-pipeline", {
          method: "POST",
          body: formData,
        });
        const data = await response.json();

        if (!response.ok) {
          throw new Error(data.detail ? JSON.stringify(data.detail, null, 2) : "Temporal Run failed");
        }

        outputSection.style.display = 'block';
        workflowIdEl.textContent = data.workflow_id;
        runIdEl.textContent = data.run_id.substring(0, 18) + "...";
        webUiBtn.href = data.temporal_web_ui_url;
        pipelineStatusEl.textContent = "COMPLETED";
        pipelineStatusEl.style.color = "#4ade80";

        valOutputEl.textContent = JSON.stringify(data.result.original_metadata, null, 2);
        prepOutputEl.textContent = JSON.stringify(data.result.preprocessed_metadata, null, 2);
        agentOutputEl.textContent = data.result.vlm_raw_analysis;
        
        statusEl.textContent = "Done";
      } catch (error) {
        outputSection.style.display = 'block';
        pipelineStatusEl.textContent = "FAILED";
        pipelineStatusEl.style.color = "#f87171";
        valOutputEl.textContent = error.message;
        prepOutputEl.textContent = "-";
        agentOutputEl.textContent = "-";
        statusEl.textContent = "Error";
      } finally {
        sendEl.disabled = false;
      }
    }

    async function sendTextPrompt() {
      const promptEl = document.getElementById("prompt");
      const sendEl = document.getElementById("text-send");
      const statusEl = document.getElementById("text-status");
      const outputEl = document.getElementById("text-output");

      const input = promptEl.value.trim();
      if (!input) {
        outputEl.textContent = "Please enter a prompt first.";
        return;
      }

      sendEl.disabled = true;
      statusEl.textContent = "Thinking...";
      outputEl.textContent = "";

      try {
        const response = await fetch("/run", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({input}),
        });
        const data = await response.json();

        if (!response.ok) {
          throw new Error(data.detail ? JSON.stringify(data.detail, null, 2) : "Request failed");
        }

        outputEl.textContent = data.output;
        statusEl.textContent = "Done";
      } catch (error) {
        outputEl.textContent = error.message;
        statusEl.textContent = "Error";
      } finally {
        sendEl.disabled = false;
      }
    }

    function handleDrop(e) {
      e.preventDefault();
      if (e.dataTransfer.files && e.dataTransfer.files[0]) {
        processFile(e.dataTransfer.files[0]);
      }
    }

    function handleFileSelect(e) {
      if (e.target.files && e.target.files[0]) {
        processFile(e.target.files[0]);
      }
    }

    function processFile(file) {
      if (!file.type.startsWith('image/')) {
        alert('Please drop an image file.');
        return;
      }
      activeFile = file;

      const reader = new FileReader();
      reader.onload = function(e) {
        const preview = document.getElementById('image-preview');
        preview.src = e.target.result;
        document.getElementById('preview-container').style.display = 'flex';
        document.getElementById('upload-text').innerHTML = `Selected file: <strong>${file.name}</strong>`;
        document.getElementById('vision-send').disabled = false;

        // Extract native dimensions
        const img = new Image();
        img.onload = function() {
          document.getElementById('meta-dimensions').textContent = `${img.width}x${img.height} px`;
        };
        img.src = e.target.result;
      };
      reader.readAsDataURL(file);
    }

    async function sendVisionPrompt() {
      if (!activeFile) return;

      const sendEl = document.getElementById("vision-send");
      const statusEl = document.getElementById("vision-status");
      const outputEl = document.getElementById("vision-output");
      const promptEl = document.getElementById("vision-prompt");

      sendEl.disabled = true;
      statusEl.textContent = "Processing image...";
      outputEl.textContent = "";

      const formData = new FormData();
      formData.append("file", activeFile);
      formData.append("prompt", promptEl.value.trim());

      try {
        const response = await fetch("/analyze-image", {
          method: "POST",
          body: formData,
        });
        const data = await response.json();

        if (!response.ok) {
          throw new Error(data.detail ? JSON.stringify(data.detail, null, 2) : "Request failed");
        }

        document.getElementById('vision-output-section').style.display = 'block';
        document.getElementById('meta-filename').textContent = data.filename;
        document.getElementById('meta-size').textContent = `${(data.processed_size_bytes / 1024).toFixed(1)} KB`;

        outputEl.textContent = data.analysis;
        statusEl.textContent = "Done";
      } catch (error) {
        outputEl.textContent = error.message;
        statusEl.textContent = "Error";
      } finally {
        sendEl.disabled = false;
      }
    }
  </script>
</body>
</html>"""

@app.get("/")
def home():
    html_content = HTML_TEMPLATE
    html_content = html_content.replace("NVIDIA NIM", PROVIDER_NAME)
    html_content = html_content.replace("an NVIDIA-hosted model", f"a {PROVIDER_NAME}-hosted model")
    html_content = html_content.replace("the NVIDIA model", f"the {PROVIDER_NAME} model")
    return HTMLResponse(html_content)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/run")
def run(req: PromptRequest):
    if not API_KEY:
        raise HTTPException(
            status_code=500,
            detail=f"{PROVIDER_NAME} API key is not set",
        )

    try:
        r = requests.post(
            API_URL,
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": API_MODEL,
                "messages": [{"role": "user", "content": req.input}],
                "temperature": 0.2,
                "max_tokens": 1024,
            },
            timeout=60,
        )
        if not r.ok:
            try:
                error_detail = r.json()
            except ValueError:
                error_detail = r.text
            raise HTTPException(
                status_code=502,
                detail={
                    f"{PROVIDER_NAME.lower().replace(' ', '_')}_status_code": r.status_code,
                    f"{PROVIDER_NAME.lower().replace(' ', '_')}_error": error_detail,
                },
            )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    data = r.json()
    output = data["choices"][0]["message"]["content"]
    return {"input": req.input, "output": output}


@app.post("/analyze-image")
async def analyze_image(
    file: UploadFile = File(...),
    prompt: str = Form("Describe this image in detail.")
):
    try:
        contents = await file.read()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read uploaded file: {str(e)}")

    # Preprocess and validate image using Pillow
    try:
        img = Image.open(io.BytesIO(contents))
        img.verify()  # Validate image headers and data structure

        # Re-open for actual processing (verify closes the handle)
        img = Image.open(io.BytesIO(contents))
        if img.mode != "RGB":
            img = img.convert("RGB")

        # Automatically downscale extremely large images to optimize payload transfer
        max_size = (1024, 1024)
        img.thumbnail(max_size, Image.Resampling.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        image_bytes = buf.getvalue()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid image format or corrupted file: {str(e)}")

    # Base64 encode the processed image
    base64_str = base64.b64encode(image_bytes).decode('utf-8')

    if not NVIDIA_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="NVIDIA_API_KEY is not set on the server backend",
        )

    try:
        r = requests.post(
            "https://integrate.api.nvidia.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {NVIDIA_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "meta/llama-3.2-11b-vision-instruct",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_str}"}}
                        ]
                    }
                ],
                "max_tokens": 1024,
                "temperature": 0.2
            },
            timeout=90
        )
        if not r.ok:
            try:
                err = r.json()
            except ValueError:
                err = r.text
            raise HTTPException(
                status_code=502,
                detail={"nvidia_status_code": r.status_code, "nvidia_error": err}
            )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    data = r.json()
    analysis = data["choices"][0]["message"]["content"]

    # Attempt to parse structured JSON from the model's text response if it exists
    structured_data = None
    try:
        start_idx = analysis.find("{")
        end_idx = analysis.rfind("}")
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            json_str = analysis[start_idx:end_idx+1]
            structured_data = json.loads(json_str)
    except Exception:
        pass

    return {
        "filename": file.filename,
        "content_type": file.content_type,
        "size_bytes": len(contents),
        "processed_size_bytes": len(image_bytes),
        "analysis": analysis,
        "structured_data": structured_data
    }


# Optional Temporal Worker (only starts if Temporal is available)
temporal_worker_task = None

if TEMPORAL_AVAILABLE:
    @app.on_event("startup")
    async def startup_event():
        """Optional: Start Temporal worker if SDK is installed"""
        print("⚠️ Temporal SDK found but workflow definitions missing. Skipping Temporal worker.")
        # Temporal worker functionality disabled - workflow definitions not available


# Temporal endpoint disabled (requires workflow definitions)
# The /run-traceable-pipeline endpoint requires external Temporal workflow definitions
# and is not available in this standalone version


# Register with AWCP Agent Registry
from awcp.agents.base import AgentSpec

def run_llama_vision(req: PromptRequest) -> dict:
    """Handler for the agent registry"""
    if not API_KEY:
        raise HTTPException(
            status_code=500,
            detail=f"{PROVIDER_NAME} API key is not set",
        )

    try:
        r = requests.post(
            API_URL,
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": API_MODEL,
                "messages": [{"role": "user", "content": req.input}],
                "temperature": 0.2,
                "max_tokens": 1024,
            },
            timeout=60,
        )
        if not r.ok:
            try:
                error_detail = r.json()
            except ValueError:
                error_detail = r.text
            raise HTTPException(
                status_code=502,
                detail={
                    f"{PROVIDER_NAME.lower().replace(' ', '_')}_status_code": r.status_code,
                    f"{PROVIDER_NAME.lower().replace(' ', '_')}_error": error_detail,
                },
            )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    data = r.json()
    output = data["choices"][0]["message"]["content"]
    return {"input": req.input, "output": output, "model": API_MODEL, "provider": PROVIDER_NAME}


AGENT = AgentSpec(
    name="llama-vision",
    route="/chat/llama-vision",
    request_model=PromptRequest,
    handler=run_llama_vision,
    runtime="nvidia"
)
