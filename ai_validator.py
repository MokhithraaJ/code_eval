# ai_validator.py
import os
import json
import textwrap
from google import genai
from google.genai import types
from dotenv import load_dotenv

api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    raise ValueError("Missing GEMINI_API_KEY! Please set it in your environment or hardcode temporarily.")

client = genai.Client(api_key=api_key)



def read_criteria(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def gather_files(project_root, include_exts=None, max_files=40, max_size_bytes=200_000):
    """
    Collect files to send to the model.
    - include_exts: list like ['.py', '.md', '.json', '.yml'] or None for all.
    - Tries to avoid extremely large files. Returns list of dicts: {'path':rel, 'content':str}
    """
    include_exts = set(include_exts) if include_exts else None
    collected = []

    for root, dirs, files in os.walk(project_root):
        # optional: skip .git, node_modules, venv
        dirs[:] = [d for d in dirs if d not in ('.git', 'node_modules', 'venv', '__pycache__')]

        for fname in files:
            if len(collected) >= max_files:
                break
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, project_root)
            _, ext = os.path.splitext(fname)
            if include_exts and ext.lower() not in include_exts:
                continue
            try:
                size = os.path.getsize(full)
                if size > max_size_bytes:
                    
                    continue
                with open(full, 'r', encoding='utf-8', errors='replace') as f:
                    content = f.read()
                collected.append({'path': rel, 'content': content})
            except Exception:
                # skip binary files and unreadable files
                continue
        if len(collected) >= max_files:
            break

    # sort to keep reproducible selection (README etc first)
    collected.sort(key=lambda x: (0 if x['path'].lower().startswith('readme') else 1, x['path']))
    return collected


PROMPT_TEMPLATE = """
You are an impartial automated code reviewer. You will be given:
1) a `criteria` JSON mapping evaluation categories to integer weights (sum may be 100).
2) a list of files in the candidate's repository (file path + file contents, limited).
3) the target role to evaluate: one of ["intern", "sde", "architect"].

Task:
- For each category in the criteria for the requested role, give the following in clear subtopics and points:
  - a numeric score from 0 to the category weight (same units as weights provided),
  - a concise written justification (2-3 sentences) clearly pointing out where it is missing or done well with an code snipet showing the expectation,
  - zero or more evidence items: each evidence item must reference a file path and (if applicable) a short quoted code snippet or line-range and explanation.
- Produce a final `total_score` (sum of category scores).
- Provide a short `overall_summary` (2-3 sentences) and a list of `suggested_actions` (3-6 actionable improvements).
- STRICTLY return a single JSON object (no extra commentary). The keys must be:
  {
    "role": "<role>",
    "criteria_scores": {
       "<category>": {"score": <number>, "justification": "<text>", "evidence": [{"path":"...","snippet":"...","note":"..."} , ...] },
       ...
    },
    "total_score": <number>,
    "overall_summary": "<text>",
    "suggested_actions": ["...","..."]
  }

Important constraints for the model:
- Do not hallucinate file names or snippets. Only cite evidence found in the provided file list.
- If a category cannot be judged from the given files, give a conservative mid/low score and say "insufficient evidence" in justification.
- Keep each justification short (<=70 words).
- Keep JSON strictly valid.

Now evaluate. Here are the inputs:

CRITERIA_JSON:
{criteria_json}

ROLE:
{role}

FILES:
{files_dump}
"""

def build_prompt(criteria, role, file_entries):
    # Prepare a brief dump of files prioritized by README, tests, main files
    limited_dump = []
    for f in file_entries:
        # include only headers if file too long
        content = f['content']
        if len(content) > 6000:
            # include first 3000 chars and last 3000 chars with marker
            content = content[:3000] + "\n\n...<<SNIPPED MIDDLE>>...\n\n" + content[-3000:]
        # escape triple backticks to avoid confusing formatting
        content = content.replace("```", "`​`​`")  # tiny escape
        limited_dump.append(f"---FILE: {f['path']}---\n{content}\n")
    files_dump = "\n".join(limited_dump)
    prompt = PROMPT_TEMPLATE.format(criteria_json=json.dumps(criteria, indent=2),
                                    role=role,
                                    files_dump=files_dump)
    # Truncate to safe length (Gemini 2.5 Flash accepts large contexts, but still be defensive)
    return prompt[:900_000]  # very large but defensive


def call_gemini(prompt, thinking_budget=0):
    """
    Call the Gemini API. Returns the raw text response (expect JSON).
    """
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_budget=thinking_budget)
        )
    )
    return response.text

def verify_evidence(project_root, report):
    """
    Verify evidence snippets returned in the model report exist in the project's actual files.
    If mismatch, mark evidence item with 'verified': False and add note.
    """
    criteria_scores = report.get('criteria_scores', {})
    for cat, details in criteria_scores.items():
        evs = details.get('evidence', [])
        new_evs = []
        for ev in evs:
            path = ev.get('path')
            snippet = ev.get('snippet', '')
            note = ev.get('note', '')
            fullpath = os.path.join(project_root, path) if path else None
            verified = False
            if fullpath and os.path.isfile(fullpath):
                try:
                    with open(fullpath, 'r', encoding='utf-8', errors='replace') as f:
                        text = f.read()
                    if snippet.strip() and snippet.strip() in text:
                        verified = True
                except Exception:
                    verified = False
            ev_copy = ev.copy()
            ev_copy['verified'] = verified
            if not verified:
                ev_copy['note'] = (ev_copy.get('note','') + " [EVIDENCE NOT FOUND]").strip()
            new_evs.append(ev_copy)
        criteria_scores[cat]['evidence'] = new_evs
    report['criteria_scores'] = criteria_scores
    return report

def validate_project(project_root, criteria_path, role='intern'):
    if role not in ('intern', 'sde', 'architect'):
        raise ValueError("role must be one of: intern, sde, architect")
    criteria = read_criteria(criteria_path)
    files = gather_files(project_root, include_exts=['.py', '.md', '.txt', '.json', '.yml', '.yaml', '.js', '.java', '.go'], max_files=80)

    prompt = build_prompt(criteria, role, files)
    # call the model (consider setting thinking_budget >0 if you want its "thinking")
    raw = call_gemini(prompt, thinking_budget=0)
    # Attempt to parse JSON strictly
    # The model is instructed to return only JSON; still be defensive:
    text = raw.strip()
    # If the model returns extra text, try to find first '{' and last '}'.
    try:
        json_start = text.index('{')
        json_end = text.rindex('}') + 1
        json_text = text[json_start:json_end]
        result = json.loads(json_text)
    except Exception:
        # fallback: try loading entire text
        try:
            result = json.loads(text)
        except Exception as e:
            raise RuntimeError(f"Failed to parse JSON from model output. Raw output first 2000 chars:\n{text[:2000]}\n\nError:{e}")

    verified_report = verify_evidence(project_root, result)

    return {
        "raw": raw,
        "result": result,
        "files_sent_count": len(files)
    }
