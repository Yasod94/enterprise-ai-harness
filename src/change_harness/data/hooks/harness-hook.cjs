#!/usr/bin/env node
// change-harness hook for the VS Code Local agent harness.
//
// Wired as agent-scoped hooks in each harness agent's frontmatter, so it only
// runs while a harness agent is active:
//   node .github/hooks/harness/harness-hook.cjs <prompt|pre-tool|post-tool|pre-compact> <scope>
//
// It owns .changes/<run>/state.json (stage, approvals) and .changes/<run>/.usage.json
// (tool-call and output budgets). Agents read those files but cannot edit them.
// Rendered by `harness sync`; do not edit in a service repo.
'use strict';

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const cp = require('child_process');

const HOOK_DIR = __dirname;
const PRE_IMPLEMENT = new Set(['orchestrator', 'check', 'intake', 'grill', 'impact', 'plan', 'scout']);
const IMPLEMENT = new Set(['implement', 'implement-task']);
const PATH_KEYS = ['filePath', 'file_path', 'path', 'paths', 'files', 'uri', 'target_file', 'targetFile',
  'filename', 'oldPath', 'newPath', 'dirPath', 'directoryPath', 'explanationFilePath'];

// ---------- small utilities ----------

function readJson(file, fallback) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return fallback; }
}

function writeJsonAtomic(file, data) {
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(data, null, 2) + '\n');
  fs.renameSync(tmp, file);
}

function sha256(file) {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

function deepMerge(base, extra) {
  if (!extra || typeof extra !== 'object' || Array.isArray(extra)) return base;
  const out = Array.isArray(base) ? base.slice() : { ...(base || {}) };
  for (const [k, v] of Object.entries(extra)) {
    out[k] = v && typeof v === 'object' && !Array.isArray(v) ? deepMerge(out[k], v) : v;
  }
  return out;
}

// Minimal YAML reader for harness.local.yaml: nested `key: value` maps only.
function parseSimpleYaml(text) {
  const root = {};
  const stack = [{ indent: -1, obj: root }];
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.replace(/\s+#.*$/, '').replace(/^#.*$/, '');
    if (!line.trim() || line.trim().startsWith('- ')) continue;
    const indent = line.length - line.trimStart().length;
    const m = line.trim().match(/^([\w.-]+):\s*(.*)$/);
    if (!m) continue;
    while (stack.length > 1 && indent <= stack[stack.length - 1].indent) stack.pop();
    const parent = stack[stack.length - 1].obj;
    const [, key, rawVal] = m;
    if (rawVal === '') {
      parent[key] = {};
      stack.push({ indent, obj: parent[key] });
    } else {
      let v = rawVal.trim().replace(/^["'](.*)["']$/, '$1');
      if (v === 'true') v = true;
      else if (v === 'false') v = false;
      else if (/^-?\d+(\.\d+)?$/.test(v)) v = Number(v);
      parent[key] = v;
    }
  }
  return root;
}

function loadConfig(root) {
  let cfg = readJson(path.join(HOOK_DIR, 'config.json'), {});
  const localFile = path.join(root, 'harness.local.yaml');
  if (fs.existsSync(localFile)) {
    try {
      const local = parseSimpleYaml(fs.readFileSync(localFile, 'utf8'));
      cfg = deepMerge(cfg, { review: local.review, budgets: local.budgets, hooks: local.hooks });
    } catch { /* a broken personal file must never block work */ }
  }
  cfg.stages = cfg.stages || ['intake', 'grill', 'impact', 'plan', 'implement', 'verify'];
  cfg.artifacts = cfg.artifacts || {};
  cfg.budgets = cfg.budgets || {};
  cfg.review = cfg.review || {};
  cfg.hooks = cfg.hooks || {};
  cfg.allowed_paths = cfg.allowed_paths || {};
  return cfg;
}

function approverName(root) {
  try {
    const name = cp.execSync('git config user.name', { cwd: root, timeout: 2000, stdio: ['ignore', 'pipe', 'ignore'] })
      .toString().trim();
    if (name) return name;
  } catch { /* fall through */ }
  return process.env.USER || process.env.USERNAME || 'unknown';
}

function rel(root, p) {
  let s = String(p).replace(/^file:\/\//, '');
  if (/^\/[A-Za-z]:\//.test(s)) s = s.slice(1); // file:///C:/...
  const abs = path.isAbsolute(s) ? s : path.join(root, s);
  return path.relative(root, abs).split(path.sep).join('/');
}

function collectPaths(input, root) {
  const found = [];
  const visit = (val, depth) => {
    if (depth > 3 || val == null) return;
    if (typeof val === 'string') { found.push(rel(root, val)); return; }
    if (Array.isArray(val)) { val.forEach((v) => visit(v, depth + 1)); return; }
    if (typeof val === 'object') {
      for (const k of PATH_KEYS) if (k in val) visit(val[k], depth + 1);
    }
  };
  if (input && typeof input === 'object') for (const k of PATH_KEYS) if (k in input) visit(input[k], 0);
  return [...new Set(found)];
}

// ---------- output helpers ----------

function emit(obj) { process.stdout.write(JSON.stringify(obj)); }

function decide(decision, reason) {
  emit({ hookSpecificOutput: { hookEventName: 'PreToolUse', permissionDecision: decision, permissionDecisionReason: reason } });
}

function addPromptContext(text) {
  emit({ hookSpecificOutput: { hookEventName: 'UserPromptSubmit', additionalContext: text } });
}

function blockPrompt(reason) {
  process.stderr.write(reason + '\n');
  process.exit(2);
}

// ---------- run state ----------

function loadRun(root) {
  const changes = path.join(root, '.changes');
  let id;
  try { id = fs.readFileSync(path.join(changes, 'current'), 'utf8').trim(); } catch { return null; }
  if (!id || /[\\/]|\.\./.test(id)) return null;
  const dir = path.join(changes, id);
  const stateFile = path.join(dir, 'state.json');
  const state = readJson(stateFile, null);
  if (!state) return null;
  state.approvals = state.approvals || {};
  const usageFile = path.join(dir, '.usage.json');
  const usage = readJson(usageFile, {});
  return { id, dir, stateFile, state, usageFile, usage, changes };
}

function saveState(run) { writeJsonAtomic(run.stateFile, run.state); }
function saveUsage(run) { writeJsonAtomic(run.usageFile, run.usage); }

function stageOf(scope) {
  if (scope === 'implement-task') return 'implement';
  return scope;
}

function budgetFor(cfg, run, key) {
  const base = (cfg.budgets[key] || {});
  const extra = (run.usage._extra || {})[key] || 0;
  return {
    calls: base.tool_calls ? base.tool_calls + extra : 0,
    bytes: base.output_kb ? base.output_kb * 1024 : 0,
  };
}

function usageOf(run, key) {
  run.usage[key] = run.usage[key] || { tool_calls: 0, output_bytes: 0 };
  return run.usage[key];
}

function artifactPaths(cfg, run, stage) {
  return (cfg.artifacts[stage] || []).map((name) => ({ name, file: path.join(run.dir, name) }));
}

// Returns null when every approved stage's files still match their recorded hashes.
function findChangedApproval(cfg, run) {
  for (const stage of cfg.stages) {
    const appr = run.state.approvals[stage];
    if (!appr || !appr.files) continue;
    for (const [name, hash] of Object.entries(appr.files)) {
      const file = path.join(run.dir, name);
      if (!fs.existsSync(file) || sha256(file) !== hash) return { stage, name };
    }
  }
  return null;
}

function planApproval(cfg, run) {
  const appr = run.state.approvals.plan;
  if (!appr) return { ok: false, reason: 'The plan is not approved yet. Edits to code unlock when the person approves plan.md.' };
  const changed = findChangedApproval(cfg, run);
  if (changed) {
    return { ok: false, reason: `${changed.name} changed after the ${changed.stage} stage was approved. The person must approve it again before code edits.` };
  }
  return { ok: true };
}

function impactFiles(run) {
  const impact = readJson(path.join(run.dir, 'impact.json'), null);
  if (!impact || !Array.isArray(impact.components)) return null;
  const files = new Set();
  for (const c of impact.components) for (const f of c.files || []) files.add(String(f).replace(/^\.\//, ''));
  return files;
}

function statusLine(cfg, run, scope) {
  const key = scope || run.state.stage;
  const used = usageOf(run, key).tool_calls;
  const b = budgetFor(cfg, run, key);
  const review = cfg.review[stageOf(key)] || 'required';
  return `Harness status: run ${run.id}, active stage ${run.state.stage}, this agent's scope ${key}, `
    + `${used} of ${b.calls || 'unlimited'} tool calls used, review mode ${review}.`;
}

// ---------- prompt: approvals, revisions, stop, extend ----------

function parsePrompt(text, scope) {
  const t = text || '';
  let m;
  if ((m = t.match(/\[harness:approve\s+([\w-]+)\]/i))) return { action: 'approve', stage: m[1].toLowerCase() };
  if ((m = t.match(/\[harness:revise\s+([\w-]+)\]/i))) return { action: 'revise', stage: m[1].toLowerCase() };
  if (/\[harness:stop\]/i.test(t)) return { action: 'stop' };
  if (/\[harness:extend\]/i.test(t) || /^\s*extend\b/i.test(t)) return { action: 'extend' };
  if ((m = t.match(/^\s*back to\s+([\w-]+)/i))) return { action: 'revise', stage: m[1].toLowerCase() };
  const userStages = ['intake', 'grill', 'impact', 'plan', 'implement', 'verify'];
  if (/^\s*approve\s*[.!]?\s*$/i.test(t)) {
    if (scope === 'implement') return { action: 'approve', stage: 'task' };
    if (userStages.includes(scope)) return { action: 'approve', stage: scope };
  }
  if (/^\s*revise\b/i.test(t) && userStages.includes(scope)) return { action: 'revise', stage: scope };
  if (/^\s*stop\s*[.!]?\s*$/i.test(t)) return { action: 'stop' };
  return null;
}

function clearLaterApprovals(cfg, run, stage, inclusive) {
  const idx = cfg.stages.indexOf(stage);
  const impl = cfg.stages.indexOf('implement');
  cfg.stages.forEach((s, i) => {
    if (i > idx || (inclusive && i === idx)) delete run.state.approvals[s];
  });
  if (idx < impl || (inclusive && idx === impl)) delete run.state.approvals.tasks;
}

function approve(cfg, run, stage, root) {
  const now = new Date().toISOString();
  if (stage === 'task') {
    run.state.approvals.tasks = (run.state.approvals.tasks || 0) + 1;
    run.usage['implement-task'] = { tool_calls: 0, output_bytes: 0 };
    saveState(run); saveUsage(run);
    return `Task approval ${run.state.approvals.tasks} recorded. Continue with the next task.`;
  }
  const idx = cfg.stages.indexOf(stage);
  if (idx < 0) blockPrompt(`Unknown stage "${stage}". Stages are: ${cfg.stages.join(', ')}.`);
  for (const earlier of cfg.stages.slice(0, idx)) {
    if (!run.state.approvals[earlier]) blockPrompt(`Approve the ${earlier} stage before ${stage}.`);
  }
  const changed = findChangedApproval(cfg, run);
  if (changed && cfg.stages.indexOf(changed.stage) < idx) {
    blockPrompt(`${changed.name} changed after the ${changed.stage} stage was approved. Go back to ${changed.stage} and approve it again.`);
  }
  const files = {};
  for (const { name, file } of artifactPaths(cfg, run, stage)) {
    if (!fs.existsSync(file) || fs.statSync(file).size === 0) {
      blockPrompt(`Cannot approve ${stage}: ${name} is missing or empty in .changes/${run.id}/.`);
    }
    files[name] = sha256(file);
  }
  if (stage === 'plan') {
    const impact = path.join(run.dir, 'impact.json');
    if (fs.existsSync(impact)) files['impact.json'] = sha256(impact);
  }
  clearLaterApprovals(cfg, run, stage, false);
  const by = approverName(root);
  run.state.approvals[stage] = { at: now, by, files };
  if (stage === 'plan') {
    writeJsonAtomic(path.join(run.dir, 'approval.json'), {
      run: run.id, approved_by: by, approved_at: now,
      plan_sha256: files['plan.md'], impact_sha256: files['impact.json'] || null,
    });
  }
  const next = cfg.stages[idx + 1] || 'done';
  run.state.stage = next;
  run.state.stage_status = next === 'done' ? 'done' : 'working';
  if (next !== 'done') run.usage[next] = { tool_calls: 0, output_bytes: 0 };
  saveState(run); saveUsage(run);
  return `Approved ${stage} by ${by}; artifact hashes recorded. Active stage is now ${next}.`;
}

function revise(cfg, run, stage) {
  if (!cfg.stages.includes(stage)) blockPrompt(`Unknown stage "${stage}". Stages are: ${cfg.stages.join(', ')}.`);
  clearLaterApprovals(cfg, run, stage, true);
  run.state.stage = stage;
  run.state.stage_status = 'working';
  saveState(run);
  return `Revising ${stage}. Approvals for ${stage} and later stages were cleared and will be needed again.`;
}

function onPrompt(cfg, root, scope, event) {
  try {
    fs.mkdirSync(path.join(root, '.changes'), { recursive: true });
    fs.writeFileSync(path.join(root, '.changes', '.hooks-ok'), new Date().toISOString().slice(0, 10) + '\n');
  } catch { /* read-only checkout */ }
  const run = loadRun(root);
  if (!run) return;
  const cmd = parsePrompt(event.prompt, scope);
  const notes = [];
  if (cmd) {
    if (cmd.action === 'approve') notes.push(approve(cfg, run, cmd.stage, root));
    else if (cmd.action === 'revise') notes.push(revise(cfg, run, cmd.stage));
    else if (cmd.action === 'stop') {
      run.state.stage_status = 'paused';
      saveState(run);
      notes.push('Run paused. Resume with the orchestrator: "resume ' + run.id + '".');
    } else if (cmd.action === 'extend') {
      const key = scope || run.state.stage;
      const base = (cfg.budgets[key] || {}).tool_calls || 0;
      run.usage._extra = run.usage._extra || {};
      run.usage._extra[key] = (run.usage._extra[key] || 0) + Math.ceil(base / 2);
      saveUsage(run);
      notes.push(`Budget for ${key} extended by ${Math.ceil(base / 2)} tool calls.`);
    }
  }
  notes.push(statusLine(cfg, run, scope));
  addPromptContext(notes.join(' '));
}

// ---------- pre-tool: budgets and edit guard ----------

function audit(cfg, root, scope, event) {
  if (!cfg.hooks.audit) return;
  try {
    const keys = Object.keys(event.tool_input || {}).join(',');
    fs.appendFileSync(path.join(root, '.changes', '.audit.log'),
      `${new Date().toISOString()}\t${scope}\t${event.tool_name}\t${keys}\n`);
  } catch { /* ignore */ }
}

function onPreTool(cfg, root, scope, event) {
  audit(cfg, root, scope, event);
  const run = loadRun(root);
  if (!run) return;
  const tool = String(event.tool_name || '');
  const input = event.tool_input || {};
  const re = (s, d) => new RegExp(s || d, 'i');
  const isEdit = re(cfg.hooks.edit_tools, '(edit|create|write|replace|insert|patch|delete|rename|move)').test(tool);
  const isTerminal = re(cfg.hooks.terminal_tools, '(terminal|runInTerminal|run_in_terminal|execute|command)').test(tool);
  const isSubagent = re(cfg.hooks.subagent_tools, '(subagent|runSubagent)').test(tool);
  const looksMcp = /^mcp_/i.test(tool) || /^[\w-]+\/[\w-]+$/.test(tool) && !/^(edit|read|search|execute|agent|web)\//.test(tool);

  // Budgets.
  const keys = scope === 'implement-task' ? ['implement-task', 'implement'] : [scope];
  for (const key of keys) {
    const b = budgetFor(cfg, run, key);
    const u = usageOf(run, key);
    if (b.calls && u.tool_calls >= b.calls) {
      return decide('deny', `Budget for ${key} reached (${u.tool_calls} of ${b.calls} tool calls). `
        + 'Stop now and show the review card with what you have. The person can type "extend" to add 50%.');
    }
    if (b.bytes && u.output_bytes >= b.bytes) {
      return decide('deny', `Output budget for ${key} reached (${Math.round(u.output_bytes / 1024)} KB). `
        + 'Stop and show the review card. Use output.sandbox for large outputs next time.');
    }
  }

  // Edit guard.
  if (isEdit && !looksMcp) {
    const paths = collectPaths(input, root);
    const runRel = `.changes/${run.id}/`;
    if (paths.length === 0 && (PRE_IMPLEMENT.has(scope) || scope === 'verify')) {
      return decide('ask', `Could not tell which file "${tool}" edits. This stage may only edit ${runRel}.`);
    }
    for (const p of paths) {
      if (p.startsWith('..')) return decide('ask', `"${p}" is outside the workspace.`);
      if (p === '.changes/current' && scope !== 'orchestrator') {
        return decide('deny', 'Only the Change Orchestrator switches the active run.');
      }
      if (p === `${runRel}state.json` || p === `${runRel}approval.json` || p === `${runRel}.usage.json`) {
        if (!(p.endsWith('state.json') && scope === 'orchestrator' && !fs.existsSync(path.join(root, p)))) {
          return decide('deny', `${p} is owned by the harness hook. Approvals and stage changes come from the person's approve, revise and back-to commands.`);
        }
      }
      if (PRE_IMPLEMENT.has(scope) || scope === 'verify') {
        const allowed = cfg.allowed_paths[scope] || cfg.allowed_paths.default || ['.changes/'];
        if (!allowed.some((a) => p === a || p.startsWith(a))) {
          return decide('deny', `The ${scope} stage can only edit ${allowed.join(', ')}. `
            + (scope === 'verify' ? 'Send code fixes back to implement.' : 'Code edits unlock when the plan is approved.'));
        }
        continue;
      }
      if (IMPLEMENT.has(scope)) {
        if (p.startsWith('.changes/')) continue;
        const pa = planApproval(cfg, run);
        if (!pa.ok) return decide('deny', pa.reason);
        const files = impactFiles(run);
        const mode = cfg.hooks.out_of_plan_files || 'ask';
        const looksTest = /(^|\/)(test|tests|spec|__tests__)\/|[._-](test|spec)\.[\w]+$|Test\.[\w]+$/.test(p);
        if (files && files.size && !files.has(p) && !looksTest && mode !== 'allow') {
          return decide(mode === 'deny' ? 'deny' : 'ask', `${p} is not in impact.json. Approve only if the plan really needs it.`);
        }
      }
    }
  }

  // Terminal commands in implement need an approved plan, since they can change files too.
  if (isTerminal && !looksMcp && IMPLEMENT.has(scope)) {
    const pa = planApproval(cfg, run);
    if (!pa.ok) return decide('deny', pa.reason);
  }

  // A new Task Runner dispatch starts a fresh per-task budget.
  if (isSubagent && scope === 'implement') run.usage['implement-task'] = { tool_calls: 0, output_bytes: 0 };

  for (const key of keys) usageOf(run, key).tool_calls += 1;
  saveUsage(run);
}

function onPostTool(cfg, root, scope, event) {
  const run = loadRun(root);
  if (!run) return;
  const resp = event.tool_response;
  const bytes = resp == null ? 0 : Buffer.byteLength(typeof resp === 'string' ? resp : JSON.stringify(resp));
  const keys = scope === 'implement-task' ? ['implement-task', 'implement'] : [scope];
  for (const key of keys) usageOf(run, key).output_bytes += bytes;
  saveUsage(run);
}

function onPreCompact(cfg, root, scope) {
  const run = loadRun(root);
  if (!run) return;
  const approvals = Object.keys(run.state.approvals).filter((k) => k !== 'tasks');
  const lines = [
    '',
    `## Status at compaction ${new Date().toISOString()}`,
    '',
    `- Active stage: ${run.state.stage} (${run.state.stage_status || 'working'}), agent scope: ${scope}`,
    `- Approved: ${approvals.join(', ') || 'none'}${run.state.approvals.tasks ? `; tasks approved: ${run.state.approvals.tasks}` : ''}`,
    `- Usage: ${Object.entries(run.usage).filter(([k]) => k !== '_extra').map(([k, v]) => `${k} ${v.tool_calls} calls`).join(', ')}`,
    '',
  ];
  fs.appendFileSync(path.join(run.dir, 'progress.md'), lines.join('\n'));
}

// ---------- main ----------

function main() {
  const [mode, scope = ''] = process.argv.slice(2);
  let event = {};
  try {
    const raw = fs.readFileSync(0, 'utf8');
    if (raw.trim()) event = JSON.parse(raw);
  } catch { /* no stdin */ }
  const root = event.cwd || process.cwd();
  const cfg = loadConfig(root);
  if (mode === 'prompt') return onPrompt(cfg, root, scope, event);
  if (mode === 'pre-tool') return onPreTool(cfg, root, scope, event);
  if (mode === 'post-tool') return onPostTool(cfg, root, scope, event);
  if (mode === 'pre-compact') return onPreCompact(cfg, root, scope, event);
}

try {
  main();
} catch (err) {
  // A bug in the hook must not stop the agent: exit 1 is a non-blocking warning.
  process.stderr.write(`change-harness hook error: ${err && err.stack || err}\n`);
  process.exit(1);
}
