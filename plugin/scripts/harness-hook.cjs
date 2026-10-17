#!/usr/bin/env node
// enterprise-ai-harness hook for the VS Code Local agent harness.
//
// Wired as agent-scoped hooks in each harness agent's frontmatter (shipped in
// the plugin), so it runs while a harness agent, or a harness subagent, is active:
//   node "${PLUGIN_ROOT}/scripts/harness-hook.cjs" <prompt|pre-tool|post-tool|pre-compact> <scope>
//
// VS Code sends snake_case JSON on stdin (cwd, session_id, prompt, tool_name,
// tool_input, tool_response). Exit 2 with a message on stderr blocks a prompt;
// PreToolUse answers with hookSpecificOutput.permissionDecision.
//
// It owns <runs>/<run>/state.json (stage, approvals), <runs>/<run>/.usage.json
// (budgets) and approval.json, keeps run files out of git with .git/info/exclude,
// and keeps the code graph current. Agents read these files but cannot edit them.
'use strict';

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const cp = require('child_process');
const os = require('os');
const codegraph = require('./codegraph-index.cjs');

const HOOK_DIR = __dirname;
const DEFAULT_RUNS_DIR = '.ai-harness';
const PRE_IMPLEMENT = new Set(['orchestrator', 'check', 'intake', 'grill', 'impact', 'plan', 'scout', 'cross-check']);
const IMPLEMENT = new Set(['implement', 'implement-task']);
const USER_STAGES = ['intake', 'grill', 'impact', 'plan', 'implement', 'verify'];
// Scopes whose budget lasts one request (reset on each prompt or dispatch) rather than one stage.
const PER_REQUEST = new Set(['orchestrator', 'check', 'scout', 'implement-task']);
const SUBSCOPES = { orchestrator: ['check'], grill: ['scout'], impact: ['scout'], implement: ['implement-task'] };
const CASE_INSENSITIVE = process.platform === 'win32' || process.platform === 'darwin';
const PATH_KEY = /^(file_?path|path|paths|files?|uri|uris|target_?file|filename|old_?path|new_?path|dir_?path|directory_?path|destination|source_?path)$/i;
const HOOK_OWNED = new Set(['state.json', 'approval.json', '.usage.json']);
const WRITEISH = /(^|[^<>&|0-9])>{1,2}(?!&)|\btee\b|\bsed\s+(-[a-z]*i|--in-place)|\bperl\s+-[a-z]*i|\b(mv|cp|rm|rmdir|truncate|touch|dd|install|ln)\s|\b(Set-Content|Add-Content|Out-File|New-Item|Remove-Item|Move-Item|Copy-Item|Rename-Item)\b|\s-delete\b|writeFile|\.write\(|open\([^)]*['"][wa]/i;
// Commands whose quoted text is code, not shell words: keep the quotes when testing them.
const INTERPRETER = /\b(sh|bash|zsh|dash|fish|cmd|node|deno|bun|python[\d.]*|perl|ruby|php|pwsh|powershell|eval|awk|gawk)\b/i;
const GIT_TREE_CHANGE = /\bgit\s+(apply|am|cherry-pick|merge|rebase|reset\s+--hard|restore|checkout\s+(--|\S+\s+--)|stash\s+(pop|apply)|pull|clean)\b/i;

// ---------- small utilities ----------

function sleep(ms) {
  try { Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms); } catch { /* best effort */ }
}

function readJson(file, fallback) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8').replace(/^\uFEFF/, '')); } catch { return fallback; }
}

function writeJsonAtomic(file, data) {
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(data, null, 2) + '\n');
  for (let i = 0; ; i++) {
    try { fs.renameSync(tmp, file); return; } catch (err) {
      // Windows refuses a rename while another process has the file open; retry briefly.
      if (i >= 8 || !['EPERM', 'EBUSY', 'EACCES'].includes(err.code)) {
        try { fs.unlinkSync(tmp); } catch { /* ignore */ }
        throw err;
      }
      sleep(25);
    }
  }
}

function sha256(file) {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

function isPlainObject(v) { return v && typeof v === 'object' && !Array.isArray(v); }

function deepMerge(base, extra) {
  if (!isPlainObject(extra)) return base;
  const out = { ...(isPlainObject(base) ? base : {}) };
  for (const [k, v] of Object.entries(extra)) {
    if (v === undefined) continue;
    out[k] = isPlainObject(v) ? deepMerge(out[k], v) : v;
  }
  return out;
}

function key(p) { return CASE_INSENSITIVE ? p.toLowerCase() : p; }

// Minimal YAML reader for personal settings: nested block-style `key: value` maps only.
function parseSimpleYaml(text) {
  const root = {};
  const stack = [{ indent: -1, obj: root }];
  for (const raw of text.replace(/^\uFEFF/, '').split(/\r?\n/)) {
    const line = raw.replace(/\s+#.*$/, '').replace(/^\s*#.*$/, '');
    if (!line.trim() || line.trim().startsWith('- ')) continue;
    const indent = line.length - line.trimStart().length;
    const m = line.trim().match(/^([\w.-]+):\s*(.*)$/);
    if (!m) continue;
    while (stack.length > 1 && indent <= stack[stack.length - 1].indent) stack.pop();
    const parent = stack[stack.length - 1].obj;
    const [, k, rawVal] = m;
    if (rawVal === '') {
      parent[k] = {};
      stack.push({ indent, obj: parent[k] });
    } else if (/^[{[]/.test(rawVal.trim())) {
      continue; // flow style is not supported; ignore the line rather than misread it
    } else {
      let v = rawVal.trim().replace(/^["'](.*)["']$/, '$1');
      if (v === 'true') v = true;
      else if (v === 'false') v = false;
      else if (/^-?\d+(\.\d+)?$/.test(v)) v = Number(v);
      parent[k] = v;
    }
  }
  return root;
}

// Personal settings may change review modes, budgets and audit logging only. Edit-tool
// patterns and the out-of-plan rule stay with the team configuration.
function personalOverrides(local) {
  const out = {};
  if (isPlainObject(local.review)) {
    out.review = {};
    for (const [k, v] of Object.entries(local.review)) if (typeof v === 'string') out.review[k] = v;
  }
  if (isPlainObject(local.budgets)) {
    out.budgets = {};
    for (const [k, v] of Object.entries(local.budgets)) {
      if (!isPlainObject(v)) continue;
      const b = {};
      if (Number(v.tool_calls) > 0) b.tool_calls = Number(v.tool_calls);
      if (Number(v.output_kb) > 0) b.output_kb = Number(v.output_kb);
      if (Object.keys(b).length) out.budgets[k] = b;
    }
  }
  if (isPlainObject(local.hooks) && typeof local.hooks.audit === 'boolean') out.hooks = { audit: local.hooks.audit };
  return out;
}

function gitTop(cwd) {
  try {
    const top = cp.execFileSync('git', ['rev-parse', '--show-toplevel'], { cwd, timeout: 3000,
      stdio: ['ignore', 'pipe', 'ignore'], windowsHide: true }).toString().trim();
    return top ? path.resolve(top) : null;
  } catch { return null; }
}

function loadConfig(root) {
  let cfg = readJson(path.join(HOOK_DIR, 'config.json'), {});
  const personal = [path.join(os.homedir(), '.copilot', `${cfg.plugin || 'enterprise-ai-harness'}.local.yaml`),
    path.join(root, 'harness.local.yaml')];
  for (const localFile of personal) {
    try {
      if (fs.existsSync(localFile)) cfg = deepMerge(cfg, personalOverrides(parseSimpleYaml(fs.readFileSync(localFile, 'utf8'))));
    } catch { /* a broken personal file must never block work */ }
  }
  cfg.stages = cfg.stages || USER_STAGES;
  cfg.artifacts = cfg.artifacts || {};
  cfg.budgets = cfg.budgets || {};
  cfg.review = cfg.review || {};
  cfg.hooks = cfg.hooks || {};
  cfg.runs = cfg.runs || {};
  cfg.runs.dir = String(cfg.runs.dir || DEFAULT_RUNS_DIR).replace(/[\\/]+$/, '');
  cfg.code_index = cfg.code_index || {};
  cfg.agent_scopes = cfg.agent_scopes || {};
  cfg.models = cfg.models || {};
  cfg.models.order = cfg.models.order || [];
  cfg.models.cross_check = cfg.models.cross_check || {};
  cfg.models.task_runners = cfg.models.task_runners || {};
  cfg.models.scope_tiers = cfg.models.scope_tiers || {};
  return cfg;
}

// Keep run files and personal settings out of git without touching any
// committed file: .git/info/exclude is local to the clone.
function ensureLocalExcludes(root, cfg) {
  let rel;
  try {
    rel = cp.execFileSync('git', ['rev-parse', '--git-path', 'info/exclude'], { cwd: root, timeout: 3000,
      stdio: ['ignore', 'pipe', 'ignore'], windowsHide: true }).toString().trim();
  } catch { return; }
  if (!rel) return;
  const file = path.isAbsolute(rel) ? rel : path.join(root, rel);
  const r = cfg.runs.dir;
  const wanted = cfg.runs.commit
    ? [`${r}/current`, `${r}/.hooks-ok`, `${r}/.sessions.json`, `${r}/.lock`, `${r}/.audit.log`, `${r}/*/.usage.json`, `${r}/*/*.tmp`]
    : [`${r}/`];
  // CodeGraph's own .codegraph/.gitignore re-includes itself; excluding the
  // directory keeps the working index entirely out of git status.
  wanted.push(`${(cfg.code_index.dir || '.codegraph').replace(/\/+$/, '')}/`, 'harness.local.yaml');
  let text = '';
  try { text = fs.readFileSync(file, 'utf8'); } catch { /* new file */ }
  const lines = text.split(/\r?\n/);
  const missing = wanted.filter((w) => !lines.includes(w));
  if (!missing.length) return;
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const header = lines.includes('# enterprise-ai-harness') ? [] : ['# enterprise-ai-harness'];
  fs.appendFileSync(file, (text && !text.endsWith('\n') ? '\n' : '') + [...header, ...missing].join('\n') + '\n');
}

function approverName(root) {
  try {
    const name = cp.execSync('git config user.name', { cwd: root, timeout: 2000, stdio: ['ignore', 'pipe', 'ignore'],
      windowsHide: true }).toString().trim();
    if (name) return name;
  } catch { /* fall through */ }
  return process.env.USER || process.env.USERNAME || 'unknown';
}

// Workspace-relative, forward-slash path for a tool argument (absolute path, relative
// path or file:// URI).
function rel(root, p) {
  let s = String(p).trim();
  if (/^file:\/\//i.test(s)) {
    s = s.replace(/^file:\/\//i, '');
    try { s = decodeURIComponent(s); } catch { /* keep as is */ }
    if (/^\/[A-Za-z]:[\\/]/.test(s)) s = s.slice(1); // file:///C:/...
  }
  const abs = path.isAbsolute(s) ? s : path.join(root, s);
  return path.relative(root, abs).split(path.sep).join('/');
}

// Every file path a tool call names: path-like keys at any depth (create_file,
// replace_string_in_file, multi_replace_string_in_file.replacements[].filePath, ...)
// and the file headers inside an apply_patch body.
function collectPaths(input, root) {
  const found = [];
  const add = (v) => { if (typeof v === 'string' && v.trim()) found.push(rel(root, v)); };
  const visit = (val, depth, pathKey) => {
    if (val == null || depth > 6) return;
    if (typeof val === 'string') {
      if (pathKey) add(val);
      else if (/^\*\*\* (Begin Patch|Update File:|Add File:|Delete File:)/m.test(val)) {
        for (const m of val.matchAll(/^\*\*\* (?:Add|Update|Delete) File:\s*(.+?)\s*$/gm)) add(m[1]);
        for (const m of val.matchAll(/^\*\*\* Move to:\s*(.+?)\s*$/gm)) add(m[1]);
      }
      return;
    }
    if (Array.isArray(val)) { val.forEach((v) => visit(v, depth + 1, pathKey)); return; }
    if (typeof val === 'object') {
      for (const [k, v] of Object.entries(val)) visit(v, depth + 1, PATH_KEY.test(k));
    }
  };
  visit(input, 0, false);
  return [...new Set(found)];
}

function commandText(input) {
  if (!input || typeof input !== 'object') return String(input || '');
  for (const k of ['command', 'cmd', 'code', 'script', 'commands']) {
    if (typeof input[k] === 'string') return input[k];
    if (Array.isArray(input[k])) return input[k].join('\n');
  }
  return JSON.stringify(input);
}

// Does a terminal or sandbox command look like it writes files? Quoted text in a plain shell
// command is data ("grep 'a > b'"), and redirects to the null device write nothing.
function writesFiles(input) {
  const text = commandText(input);
  const lang = String((input && typeof input === 'object' && input.language) || 'shell').toLowerCase();
  let t = text;
  if (/^(shell|bash|sh|zsh|powershell|pwsh|cmd)$/.test(lang) && !INTERPRETER.test(text)) {
    t = t.replace(/'[^']*'|"(?:[^"\\]|\\.)*"/g, '""');
  }
  t = t.replace(/[\d&]?>{1,2}\s*(\/dev\/null|\$null|NUL)\b|\d>&\d/gi, ' ');
  return WRITEISH.test(t);
}

// True when every path a command names is inside the runs folder.
function onlyRunPaths(cfg, text) {
  const rq = cfg.runs.dir.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const runPath = new RegExp(`(^|[\\s'"=])\\.?[\\\\/]?${rq}([\\\\/][^\\s'"|;&<>]*)?`, 'g');
  if (!runPath.test(text)) return false;
  const rest = text.replace(runPath, '$1');
  return !/(^|[\s'"=])(?!-)[^\s'"|;&<>]*([\/\\*]|\.[A-Za-z0-9]{1,8}\b)/.test(rest);
}

// ---------- output helpers ----------

function emit(obj) { process.stdout.write(JSON.stringify(obj)); }

function decide(decision, reason) {
  emit({ hookSpecificOutput: { hookEventName: 'PreToolUse', permissionDecision: decision, permissionDecisionReason: reason } });
}

function addPromptContext(text) {
  emit({ hookSpecificOutput: { hookEventName: 'UserPromptSubmit', additionalContext: text } });
}

class Block extends Error {}

function blockPrompt(reason) { throw new Block(reason); }

// ---------- locking ----------

// A short exclusive lock around read-modify-write of the run files, so parallel
// tool calls do not lose counter updates. Never blocks work: after ~3 s it proceeds.
function acquireLock(dir) {
  const lock = path.join(dir, '.lock');
  try { fs.mkdirSync(dir, { recursive: true }); } catch { return null; }
  const deadline = Date.now() + 3000;
  for (;;) {
    try {
      const fd = fs.openSync(lock, 'wx');
      fs.writeSync(fd, String(process.pid));
      fs.closeSync(fd);
      return lock;
    } catch (err) {
      if (err.code !== 'EEXIST') return null;
      try {
        if (Date.now() - fs.statSync(lock).mtimeMs > 10000) { fs.unlinkSync(lock); continue; }
      } catch { continue; }
      if (Date.now() > deadline) return null;
      sleep(15);
    }
  }
}

function releaseLock(lock) {
  if (lock) { try { fs.unlinkSync(lock); } catch { /* ignore */ } }
}

// ---------- run state ----------

function runsPath(root, cfg) { return path.join(root, cfg.runs.dir); }

// { status: 'none' } | { status: 'broken', id, reason } | { status: 'ok', ...run }
function loadRun(root, cfg) {
  const runs = runsPath(root, cfg);
  let id;
  try { id = fs.readFileSync(path.join(runs, 'current'), 'utf8').replace(/^\uFEFF/, '').trim(); } catch { return { status: 'none' }; }
  if (!id) return { status: 'none' };
  if (/[\\/]|\.\./.test(id)) return { status: 'broken', id, reason: `${cfg.runs.dir}/current names an invalid run id "${id}"` };
  const dir = path.join(runs, id);
  const stateFile = path.join(dir, 'state.json');
  if (!fs.existsSync(stateFile)) return { status: 'broken', id, reason: `${cfg.runs.dir}/${id}/state.json is missing` };
  const state = readJson(stateFile, null);
  if (!isPlainObject(state)) return { status: 'broken', id, reason: `${cfg.runs.dir}/${id}/state.json cannot be read` };
  state.approvals = isPlainObject(state.approvals) ? state.approvals : {};
  const usageFile = path.join(dir, '.usage.json');
  const usage = readJson(usageFile, {});
  return { status: 'ok', id, dir, stateFile, state, usageFile, usage: isPlainObject(usage) ? usage : {}, runs };
}

function saveState(run) { writeJsonAtomic(run.stateFile, run.state); }
function saveUsage(run) { writeJsonAtomic(run.usageFile, run.usage); }

function stageOf(scope) {
  if (scope === 'implement-task') return 'implement';
  if (scope === 'scout') return 'impact';
  return scope;
}

function budgetFor(cfg, run, k) {
  const base = cfg.budgets[k] || {};
  const extraCalls = ((run.usage._extra || {})[k]) || 0;
  const extraBytes = ((run.usage._extra_bytes || {})[k]) || 0;
  return {
    calls: base.tool_calls ? base.tool_calls + extraCalls : 0,
    bytes: base.output_kb ? base.output_kb * 1024 + extraBytes : 0,
  };
}

function usageOf(run, k) {
  if (!isPlainObject(run.usage[k])) run.usage[k] = { tool_calls: 0, output_bytes: 0 };
  return run.usage[k];
}

function resetUsage(run, k) {
  run.usage[k] = { tool_calls: 0, output_bytes: 0 };
  if (run.usage._extra) delete run.usage._extra[k];
  if (run.usage._extra_bytes) delete run.usage._extra_bytes[k];
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
  for (const c of impact.components) for (const f of (c && c.files) || []) files.add(key(String(f).replace(/^\.\//, '')));
  return files;
}

// Task ids and tiers from plan.md: "### T3. <title>" followed by "- Tier: deep".
function planTasks(run) {
  const tasks = new Map();
  let text;
  try { text = fs.readFileSync(path.join(run.dir, 'plan.md'), 'utf8'); } catch { return tasks; }
  let task = null;
  for (const line of text.split(/\r?\n/)) {
    const h = line.match(/^#{2,4}\s+(T\d+)\b/);
    if (h) { task = h[1].toUpperCase(); tasks.set(task, null); continue; }
    if (/^#{1,4}\s/.test(line)) { task = null; continue; }
    const t = task && line.match(/^\s*[-*]\s*(?:\*\*)?Tier(?:\*\*)?:\s*(?:\*\*)?([\w-]+)/i);
    if (t) tasks.set(task, t[1].toLowerCase());
  }
  return tasks;
}

function batchSize(cfg) {
  const mode = String(cfg.review.implement || 'per-task');
  const m = mode.match(/per-batch-(\d+)/);
  if (m) return Math.max(1, Number(m[1]));
  return mode === 'none' ? Infinity : 1;
}

// ---------- sessions: which run a chat works on ----------

function sessionsFile(root, cfg) { return path.join(runsPath(root, cfg), '.sessions.json'); }

function boundRun(root, cfg, sessionId) {
  if (!sessionId) return null;
  const rec = (readJson(sessionsFile(root, cfg), {}) || {})[sessionId];
  return rec ? rec.run : null;
}

function bindSession(root, cfg, sessionId, runId) {
  if (!sessionId || !runId) return;
  const file = sessionsFile(root, cfg);
  const all = readJson(file, {}) || {};
  all[sessionId] = { run: runId, at: new Date().toISOString() };
  const entries = Object.entries(all).sort((a, b) => String(b[1].at).localeCompare(String(a[1].at))).slice(0, 50);
  try { writeJsonAtomic(file, Object.fromEntries(entries)); } catch { /* best effort */ }
}

// ---------- model tiers and cross-checks ----------

function tierRank(cfg, tier) { return cfg.models.order.indexOf(tier); }

function modelOf(cfg, tier) {
  const m = (cfg.models.first_model || {})[tier];
  return m ? ` (${m})` : '';
}

// The tier of this agent session: what the person chose with a "Continue on the
// <tier> model" button in this chat, else the agent's configured tier.
function sessionTier(cfg, run, scope, event) {
  const rec = (run.state.session_tiers || {})[scope];
  const fresh = rec && Date.now() - Date.parse(rec.at || 0) < 12 * 3600 * 1000;
  if (fresh && rec.session && event.session_id && rec.session === event.session_id) return rec.tier;
  return cfg.models.scope_tiers[scope] || null;
}

function checkFile(run, stage) { return path.join(run.dir, `${stage}-check.md`); }

function checkVerdict(file) {
  try {
    const m = fs.readFileSync(file, 'utf8').match(/^\s*Verdict:\s*(.+?)\s*$/im);
    return m ? m[1].toLowerCase() : 'no verdict line';
  } catch { return null; }
}

function startCrossCheck(cfg, run, stage) {
  if (!cfg.stages.includes(stage)) blockPrompt(`Unknown stage "${stage}". Stages are: ${cfg.stages.join(', ')}.`);
  const files = {};
  for (const { name, file } of artifactPaths(cfg, run, stage)) {
    if (!fs.existsSync(file) || fs.statSync(file).size === 0) {
      blockPrompt(`Nothing to cross-check yet: ${name} is missing or empty in ${cfg.runs.dir}/${run.id}/.`);
    }
    files[name] = sha256(file);
  }
  const spec = cfg.models.cross_check[stage] || {};
  run.state.cross_checks = run.state.cross_checks || {};
  run.state.cross_checks[stage] = { at: new Date().toISOString(), checker: spec.checker || null, checked: files };
  resetUsage(run, 'cross-check');
  const names = Object.keys(files).join(', ');
  return `Cross-check of ${stage} started on the ${spec.checker || 'chosen'} tier; ${names} hashed as checked. `
    + `Write the findings to ${cfg.runs.dir}/${run.id}/${stage}-check.md, starting with a "Verdict:" line.`;
}

// Returns the cross-check record for an approval, or blocks the approval when a
// required check has not run and the person did not ask to skip it.
function crossCheckRecord(cfg, run, stage, files, skip, by) {
  const spec = cfg.models.cross_check[stage];
  if (!spec) return null;
  const rec = (run.state.cross_checks || {})[stage];
  const file = checkFile(run, stage);
  const done = rec && fs.existsSync(file) && fs.statSync(file).size > 0;
  if (done) {
    const changed = Object.entries(rec.checked || {}).some(([name, hash]) => files[name] !== hash);
    return { checker: rec.checker, verdict: checkVerdict(file), checked_at: rec.at,
      changed_after_check: changed, check_sha256: sha256(file) };
  }
  if (spec.required && !skip) {
    blockPrompt(`The ${stage} needs a cross-check on the ${spec.checker} tier before approval. In the ${stage} agent, press `
      + `"Cross-check with the ${spec.checker} model", or "Approve and skip the cross-check" to record that you skipped it.`);
  }
  return skip ? { skipped: true, by, required: !!spec.required } : null;
}

function describeCheck(stage, cc) {
  if (!cc) return '';
  if (cc.skipped) return ` Cross-check of ${stage} skipped by ${cc.by}; recorded.`;
  return ` Cross-check verdict: ${cc.verdict}${cc.changed_after_check ? `; ${stage} changed after the check` : ''}.`;
}

function statusLine(cfg, run, scope, event) {
  const k = scope || run.state.stage;
  const used = usageOf(run, k).tool_calls;
  const b = budgetFor(cfg, run, k);
  const review = cfg.review[stageOf(k)] || 'required';
  let line = `Harness status: run ${run.id}, active stage ${run.state.stage}`
    + `${run.state.stage_status === 'paused' ? ' (paused)' : ''}, this agent's scope ${k}, `
    + `${used} of ${b.calls || 'unlimited'} tool calls used, review mode ${review}, `
    + `run files ${cfg.runs.commit ? 'committed with the PR' : 'local only'}.`;
  const tier = sessionTier(cfg, run, k, event);
  if (tier) line += ` Model tier: ${tier}${modelOf(cfg, tier)}.`;
  const spec = cfg.models.cross_check[k];
  if (spec && spec.required && !(run.state.cross_checks || {})[k]) {
    line += ` A cross-check on the ${spec.checker} tier is required before ${k} can be approved.`;
  }
  return line;
}

// ---------- prompt: approvals, revisions, stop, extend ----------

// Buttons send prompts that start with a [harness:...] marker; typed commands must be
// the whole prompt (or start it, for revise and back to), so a sentence that only
// mentions a marker or the word "approve" does nothing.
function parsePrompt(text, scope) {
  const t = String(text || '').replace(/^\uFEFF/, '');
  const m = t.match(/^\s*\[harness:([\w-]+)(?:\s+([\w-]+))?\]\s*([\s\S]*)$/i);
  if (m) {
    const action = m[1].toLowerCase();
    const arg = (m[2] || '').toLowerCase();
    const rest = m[3] || '';
    const skip = /^skip (the )?cross-?check\b/i.test(rest.trim());
    if (action === 'approve') return { action: 'approve', stage: arg, skip };
    if (['revise', 'cross-check', 'tier'].includes(action)) return { action, stage: arg, tier: arg };
    if (['stop', 'extend', 'codegraph-save'].includes(action)) return { action };
    return { action: 'unknown', marker: action };
  }
  const s = t.trim();
  let w;
  if (/^approve\s*[.!]?$/i.test(s) || (w = s.match(/^approve\b[\s,.;:-]*(and\s+)?skip (the )?cross-?check\s*[.!]?$/i))) {
    if (scope === 'implement') return { action: 'approve', stage: 'task' };
    if (USER_STAGES.includes(scope)) return { action: 'approve', stage: scope, skip: !!w };
    return null;
  }
  if ((w = s.match(/^(?:go\s+)?back to (?:the\s+)?([a-z-]+)\b/i)) && USER_STAGES.includes(w[1].toLowerCase())) {
    return { action: 'revise', stage: w[1].toLowerCase() };
  }
  if (/^revise\b/i.test(s) && USER_STAGES.includes(scope)) return { action: 'revise', stage: scope };
  if (/^stop\s*[.!]?$/i.test(s)) return { action: 'stop' };
  if (/^extend\s*[.!]?$/i.test(s)) return { action: 'extend' };
  if (/^(save|refresh) (the )?code ?graph\s*[.!]?$/i.test(s)) return { action: 'codegraph-save' };
  if (/^resume\b/i.test(s) && scope === 'orchestrator') return { action: 'resume' };
  return null;
}

function clearLaterApprovals(cfg, run, stage, inclusive) {
  const idx = cfg.stages.indexOf(stage);
  const impl = cfg.stages.indexOf('implement');
  cfg.stages.forEach((s, i) => {
    if (i > idx || (inclusive && i === idx)) delete run.state.approvals[s];
    // A check of a later stage looked at artifacts built on what is being revised.
    if (i > idx && run.state.cross_checks) delete run.state.cross_checks[s];
  });
  if (idx < impl || (inclusive && idx === impl)) {
    delete run.state.approvals.tasks;
    delete run.state.tasks_dispatched;
  }
}

function approveTasksCheck(cfg, run) {
  // Before implement is approved: every planned task ran, and all but the last batch were reviewed.
  const planned = [...planTasks(run).keys()];
  const dispatched = run.state.tasks_dispatched || [];
  const missing = planned.filter((t) => !dispatched.includes(t));
  if (planned.length && missing.length) {
    blockPrompt(`Cannot approve implement: ${missing.join(', ')} ${missing.length === 1 ? 'has' : 'have'} not run yet. `
      + 'Continue with the Implementer, or go back to plan to change the task list.');
  }
  const batch = batchSize(cfg);
  const reviewed = (run.state.approvals.tasks || 0) * batch;
  if (Number.isFinite(batch) && dispatched.length > reviewed + batch) {
    blockPrompt(`Cannot approve implement: ${dispatched.length} tasks ran but only ${run.state.approvals.tasks || 0} task reviews `
      + 'were recorded. Review the remaining tasks with "Approve and continue" first.');
  }
}

function approve(cfg, run, stage, root, skip) {
  const now = new Date().toISOString();
  if (stage === 'task') {
    run.state.approvals.tasks = (run.state.approvals.tasks || 0) + 1;
    resetUsage(run, 'implement-task');
    return `Task review ${run.state.approvals.tasks} recorded. Continue with the next task.`;
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
      blockPrompt(`Cannot approve ${stage}: ${name} is missing or empty in ${cfg.runs.dir}/${run.id}/.`);
    }
    files[name] = sha256(file);
  }
  if (stage === 'plan') {
    const impact = path.join(run.dir, 'impact.json');
    if (fs.existsSync(impact)) files['impact.json'] = sha256(impact);
  }
  if (stage === 'implement') approveTasksCheck(cfg, run);
  const by = approverName(root);
  const cc = crossCheckRecord(cfg, run, stage, files, skip, by);
  clearLaterApprovals(cfg, run, stage, false);
  run.state.approvals[stage] = { at: now, by, files };
  if (cc) run.state.approvals[stage].cross_check = cc;
  if (stage === 'plan') {
    writeJsonAtomic(path.join(run.dir, 'approval.json'), {
      run: run.id, approved_by: by, approved_at: now,
      plan_sha256: files['plan.md'], impact_sha256: files['impact.json'] || null,
      cross_check: cc,
    });
  }
  const next = cfg.stages[idx + 1] || 'done';
  run.state.stage = next;
  run.state.stage_status = next === 'done' ? 'done' : 'working';
  if (next !== 'done') {
    resetUsage(run, next);
    for (const sub of SUBSCOPES[next] || []) resetUsage(run, sub);
  }
  return `Approved ${stage} by ${by}; artifact hashes recorded.${describeCheck(stage, cc)} Active stage is now ${next}.`;
}

function revise(cfg, run, stage) {
  if (!cfg.stages.includes(stage)) blockPrompt(`Unknown stage "${stage}". Stages are: ${cfg.stages.join(', ')}.`);
  clearLaterApprovals(cfg, run, stage, true);
  run.state.stage = stage;
  run.state.stage_status = 'working';
  resetUsage(run, stage);
  for (const sub of SUBSCOPES[stage] || []) resetUsage(run, sub);
  return `Revising ${stage}. Approvals for ${stage} and later stages were cleared and will be needed again; its budget starts over.`;
}

function extend(cfg, run, scope) {
  const k = scope || run.state.stage;
  const added = [];
  for (const target of [k, ...(SUBSCOPES[k] || [])]) {
    const base = cfg.budgets[target] || {};
    if (base.tool_calls) {
      run.usage._extra = run.usage._extra || {};
      run.usage._extra[target] = (run.usage._extra[target] || 0) + Math.ceil(base.tool_calls / 2);
    }
    if (base.output_kb) {
      run.usage._extra_bytes = run.usage._extra_bytes || {};
      run.usage._extra_bytes[target] = (run.usage._extra_bytes[target] || 0) + Math.ceil(base.output_kb / 2) * 1024;
    }
    if (base.tool_calls || base.output_kb) added.push(target);
  }
  return added.length ? `Budgets for ${added.join(', ')} extended by 50%.` : `No budget is set for ${k}.`;
}

function codegraphSave(cfg, root, reason) {
  try { return codegraph.save(root, cfg.code_index, reason); } catch (err) { return `Code graph seed not saved: ${err.message || err}`; }
}

function onPrompt(cfg, root, scope, event) {
  const notes = [];
  const runs = runsPath(root, cfg);
  try {
    fs.mkdirSync(runs, { recursive: true });
    fs.writeFileSync(path.join(runs, '.hooks-ok'), new Date().toISOString() + '\n');
    ensureLocalExcludes(root, cfg);
  } catch { /* read-only checkout */ }
  try {
    const restored = codegraph.restore(root, cfg.code_index);
    if (restored) notes.push(restored);
  } catch (err) { notes.push(`Code graph seed could not be restored: ${err.message || err}`); }

  const cmd = parsePrompt(event.prompt, scope);
  let saveReason = cmd && cmd.action === 'codegraph-save' ? 'requested' : null;
  const lock = acquireLock(runs);
  try {
    const run = loadRun(root, cfg);
    const stateful = cmd && !['codegraph-save', 'stop', 'resume'].includes(cmd.action);
    if (run.status !== 'ok') {
      if (stateful && cmd.action !== 'unknown') {
        blockPrompt(run.status === 'none'
          ? 'There is no active run. Start one with the Change Orchestrator (a Jira key or a sentence describing the change).'
          : `The active run cannot be used: ${run.reason}. Ask the Change Orchestrator to resume a run or start a new one.`);
      }
      notes.push(run.status === 'none'
        ? `Harness hook active (${cfg.plugin || 'enterprise-ai-harness'} ${cfg.version || ''}). No active run.`
        : `Harness hook active. The active run cannot be used: ${run.reason}.`);
      return { notes, saveReason };
    }

    // A chat stays with the run it started on, unless the orchestrator switches it.
    const bound = boundRun(root, cfg, event.session_id);
    if (scope !== 'orchestrator' && bound && bound !== run.id && stateful) {
      blockPrompt(`This chat was working on run ${bound}, but the active run is now ${run.id} (changed in another chat). `
        + `Ask the Change Orchestrator to "resume ${bound}", or continue ${run.id} in a new chat.`);
    }
    if (!bound) bindSession(root, cfg, event.session_id, run.id);
    if (PER_REQUEST.has(scope)) resetUsage(run, scope);

    if (cmd) {
      if (cmd.action === 'unknown') blockPrompt(`Unknown harness command "[harness:${cmd.marker}]".`);
      else if (cmd.action === 'cross-check') notes.push(startCrossCheck(cfg, run, cmd.stage));
      else if (cmd.action === 'tier') {
        if (!cfg.models.order.includes(cmd.tier)) blockPrompt(`Unknown model tier "${cmd.tier}". Tiers: ${cfg.models.order.join(', ') || 'none configured'}.`);
        run.state.session_tiers = run.state.session_tiers || {};
        run.state.session_tiers[scope] = { tier: cmd.tier, session: event.session_id || null, at: new Date().toISOString() };
        notes.push(`This ${scope} session now runs on the ${cmd.tier} tier${modelOf(cfg, cmd.tier)}. Task Runners up to ${cmd.tier} can run.`);
      } else if (cmd.action === 'approve') {
        notes.push(approve(cfg, run, cmd.stage, root, cmd.skip));
        if (cmd.stage === 'verify' && codegraph.shouldRefresh(root, cfg.code_index)) saveReason = `pr ${run.id}`;
      } else if (cmd.action === 'revise') notes.push(revise(cfg, run, cmd.stage));
      else if (cmd.action === 'stop') {
        run.state.stage_status = 'paused';
        notes.push(`Run paused. Harness agents will not run tools until the person asks the Change Orchestrator to "resume ${run.id}".`);
      } else if (cmd.action === 'resume') {
        if (run.state.stage_status === 'paused') run.state.stage_status = 'working';
      } else if (cmd.action === 'extend') notes.push(extend(cfg, run, scope));
      if (cmd.action === 'approve' || cmd.action === 'revise') {
        if (run.state.stage_status === 'paused') run.state.stage_status = 'working';
      }
    }
    saveState(run); saveUsage(run);
    notes.push(statusLine(cfg, run, scope, event));
    if (saveReason === `pr ${run.id}`) notes.push('Refreshing the code graph seed; commit .codegraph-seed/ with the PR as its own commit, "Refresh code graph seed".');
    return { notes, saveReason };
  } finally {
    releaseLock(lock);
  }
}

// ---------- pre-tool: budgets, pause, edit and command guard ----------

function audit(cfg, root, scope, event) {
  if (!cfg.hooks.audit) return;
  try {
    const keys = Object.keys(event.tool_input || {}).join(',');
    fs.appendFileSync(path.join(runsPath(root, cfg), '.audit.log'),
      `${new Date().toISOString()}\t${scope}\t${event.tool_name}\t${keys}\n`);
  } catch { /* ignore */ }
}

function toolKinds(cfg, tool) {
  const re = (s, d) => { try { return new RegExp(s || d, 'i'); } catch { return new RegExp(d, 'i'); } };
  const looksMcp = /^mcp_/i.test(tool) || (/^[\w.-]+\/[\w.-]+$/.test(tool) && !/^(edit|read|search|execute|agent|web|vscode|todo)\//i.test(tool));
  const isEdit = !looksMcp && re(cfg.hooks.edit_tools, '(edit|create|write|replace|insert|patch|delete|rename|move)').test(tool);
  const isTerminal = (!looksMcp && re(cfg.hooks.terminal_tools, '(terminal|runInTerminal|run_in_terminal|execute|command|task)').test(tool))
    || (looksMcp && /(execute|exec|shell|bash|run_?command|batch_execute)/i.test(tool));
  const isSubagent = re(cfg.hooks.subagent_tools, '(subagent|runSubagent)').test(tool);
  return { looksMcp, isEdit, isTerminal, isSubagent };
}

// Is a workspace-relative path one the hook owns (state, approvals, usage, checks, sessions)?
function protectedPath(cfg, p, scope) {
  const r = key(cfg.runs.dir) + '/';
  const pk = key(p);
  const cg = key((cfg.code_index.dir || '.codegraph').replace(/\/+$/, '')) + '/';
  const seed = key((cfg.code_index.seed_dir || '.codegraph-seed').replace(/\/+$/, '')) + '/';
  if (pk.startsWith(cg) || pk.startsWith(seed) || pk + '/' === seed) {
    return `${p} is maintained by the harness hook and CodeGraph. Say "save code graph" to refresh the seed.`;
  }
  if (pk === 'harness.local.yaml') return 'harness.local.yaml holds the person\'s own settings; agents do not edit it.';
  if (!pk.startsWith(r)) return null;
  const restPath = pk.slice(r.length);
  if (restPath === 'current') return scope === 'orchestrator' ? null : 'Only the Change Orchestrator switches the active run.';
  if (['.sessions.json', '.lock', '.hooks-ok'].includes(restPath)) return `${p} is owned by the harness hook.`;
  const parts = restPath.split('/');
  if (parts.length === 2 && /^[\w.-]+-check\.md$/.test(parts[1]) && scope !== 'cross-check') {
    return `${p} is written by the Checker on the stronger model. Record what you did with its findings in the stage's own artifact.`;
  }
  if (parts.length === 2 && HOOK_OWNED.has(parts[1])) return 'owned';
  return null;
}

function onPreTool(cfg, root, scope, event) {
  audit(cfg, root, scope, event);
  const runs = runsPath(root, cfg);
  const lock = acquireLock(runs);
  try { return preTool(cfg, root, scope, event); } finally { releaseLock(lock); }
}

function preTool(cfg, root, scope, event) {
  const tool = String(event.tool_name || '');
  const input = event.tool_input || {};
  const { isEdit, isTerminal, isSubagent } = toolKinds(cfg, tool);
  const run = loadRun(root, cfg);
  const runId = run.status === 'ok' ? run.id : null;
  const runRel = runId ? `${cfg.runs.dir}/${runId}/` : `${cfg.runs.dir}/`;

  // Commands that write hook-owned files are refused in every scope.
  if (isTerminal) {
    const text = commandText(input);
    const rq = cfg.runs.dir.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const touchesOwned = new RegExp(`${rq}[\\\\/][^\\s'"]*(state\\.json|approval\\.json|\\.usage\\.json|-check\\.md|\\.sessions\\.json|current)`
      + '|\\.codegraph-seed|harness\\.local\\.yaml', 'i').test(text);
    if (touchesOwned && writesFiles(input)) {
      return decide('deny', 'This command writes files the harness hook owns (run state, approvals, checks, the code graph seed or personal settings).');
    }
  }

  if (run.status !== 'ok') {
    // Without a usable run, harness agents may only create run files.
    if (isEdit) {
      for (const p of collectPaths(input, root)) {
        const why = protectedPath(cfg, p, scope);
        if (why && why !== 'owned') return decide('deny', why);
        if (why === 'owned' && !(scope === 'orchestrator' && p.endsWith('state.json') && !fs.existsSync(path.join(root, p)))) {
          return decide('deny', `${p} is owned by the harness hook.`);
        }
        if (!key(p).startsWith(key(cfg.runs.dir) + '/')) {
          return decide('deny', run.status === 'none'
            ? 'There is no active run, so harness agents can only create run files. Start a run with the Change Orchestrator.'
            : `The active run cannot be used (${run.reason}). Ask the Change Orchestrator to resume a run or start a new one.`);
        }
      }
    }
    if (isTerminal && IMPLEMENT.has(scope)) return decide('deny', 'There is no usable active run, so the plan is not approved.');
    return;
  }

  if (run.state.stage_status === 'paused' && scope !== 'orchestrator') {
    return decide('deny', `Run ${run.id} is paused. Stop and tell the person to ask the Change Orchestrator to "resume ${run.id}".`);
  }
  const bound = boundRun(root, cfg, event.session_id);
  if (bound && bound !== run.id && scope !== 'orchestrator' && (isEdit || isTerminal)) {
    return decide('deny', `This chat works on run ${bound}, but the active run is now ${run.id}. Stop and tell the person.`);
  }

  // Budgets for this agent's scope.
  const b = budgetFor(cfg, run, scope);
  const u = usageOf(run, scope);
  if (b.calls && u.tool_calls >= b.calls) {
    return decide('deny', `Budget for ${scope} reached (${u.tool_calls} of ${b.calls} tool calls). `
      + 'Stop now and show the review card with what you have. The person can type "extend" to add 50%.');
  }
  if (b.bytes && u.output_bytes >= b.bytes) {
    return decide('deny', `Output budget for ${scope} reached (${Math.round(u.output_bytes / 1024)} KB). `
      + 'Stop and show the review card. Use output.sandbox for large outputs; the person can type "extend".');
  }

  // Edit guard.
  if (isEdit) {
    const paths = collectPaths(input, root);
    if (paths.length === 0) {
      if (PRE_IMPLEMENT.has(scope) || scope === 'verify') {
        return decide('ask', `Could not tell which file "${tool}" edits. This stage may only edit ${runRel}.`);
      }
      if (IMPLEMENT.has(scope)) {
        const pa = planApproval(cfg, run);
        if (!pa.ok) return decide('deny', pa.reason);
      }
    }
    for (const p of paths) {
      if (p.startsWith('..') || path.isAbsolute(p)) return decide('ask', `"${p}" is outside the workspace.`);
      const why = protectedPath(cfg, p, scope);
      if (why === 'owned') {
        if (!(scope === 'orchestrator' && p.endsWith('state.json') && !fs.existsSync(path.join(root, p)))) {
          return decide('deny', `${p} is owned by the harness hook. Approvals and stage changes come from the person's buttons and commands.`);
        }
        continue;
      }
      if (why) return decide('deny', why);
      const inRuns = key(p).startsWith(key(cfg.runs.dir) + '/');
      if (scope === 'cross-check' && !(key(p).startsWith(key(runRel)) && /-check\.md$/i.test(p))) {
        return decide('deny', `A Checker only writes its <stage>-check.md in ${runRel}; the stage agent applies the findings.`);
      }
      if (PRE_IMPLEMENT.has(scope) || scope === 'verify') {
        if (!inRuns) {
          return decide('deny', `The ${scope} stage can only edit ${cfg.runs.dir}/. `
            + (scope === 'verify' ? 'Send code fixes back to implement.' : 'Code edits unlock when the plan is approved.'));
        }
        continue;
      }
      if (IMPLEMENT.has(scope)) {
        if (inRuns) continue;
        const pa = planApproval(cfg, run);
        if (!pa.ok) return decide('deny', pa.reason);
        const files = impactFiles(run);
        const mode = cfg.hooks.out_of_plan_files || 'ask';
        const looksTest = /(^|\/)(test|tests|spec|specs|__tests__)\/|[._-](test|spec)s?\.[\w]+$|Tests?\.[\w]+$/.test(p);
        if (files && files.size && !files.has(key(p)) && !looksTest && mode !== 'allow') {
          return decide(mode === 'deny' ? 'deny' : 'ask', `${p} is not in impact.json. Approve only if the plan really needs it.`);
        }
      }
    }
  }

  // Terminal and sandbox commands can change files too.
  if (isTerminal) {
    const text = commandText(input);
    if (IMPLEMENT.has(scope)) {
      const pa = planApproval(cfg, run);
      if (!pa.ok) return decide('deny', pa.reason);
    } else if ((PRE_IMPLEMENT.has(scope) || scope === 'verify') && (writesFiles(input) || GIT_TREE_CHANGE.test(text))) {
      const onlyRuns = onlyRunPaths(cfg, text);
      if (!onlyRuns || GIT_TREE_CHANGE.test(text)) {
        return decide('ask', `This command may change files outside ${cfg.runs.dir}/, and the ${scope} stage does not change code. `
          + 'Allow it only if it does not modify source files.');
      }
    }
  }

  // Subagent dispatch: fresh budget for the subagent; Task Runner tier and review cadence.
  if (isSubagent) {
    const name = String(input.agentName || input.agent_name || input.agent || '');
    const subScope = cfg.agent_scopes[name];
    if (subScope && PER_REQUEST.has(subScope)) resetUsage(run, subScope);
    if (scope === 'implement' && subScope === 'implement-task') {
      const text = String(input.prompt || '');
      const task = ((text.match(/^\s*\W{0,3}(T\d+)\b/i) || text.match(/\b(T\d+)\b/) || String(input.description || '').match(/\b(T\d+)\b/) || [])[1] || '').toUpperCase();
      const pa = planApproval(cfg, run);
      if (!pa.ok) return decide('deny', pa.reason);
      const tasks = planTasks(run);
      if (tasks.size) {
        if (!task) return decide('deny', 'Start the Task Runner prompt with the task id from plan.md (for example "T3: ...").');
        if (!tasks.has(task)) return decide('deny', `${task} is not a task in the approved plan.md.`);
        const dispatched = run.state.tasks_dispatched || [];
        const batch = batchSize(cfg);
        const allowed = (run.state.approvals.tasks || 0) * batch + batch;
        if (!dispatched.includes(task) && Number.isFinite(batch) && dispatched.length >= allowed) {
          return decide('deny', `The person has not reviewed the previous task${batch > 1 ? 's' : ''} yet. `
            + 'Stop and show the review card; the next task starts after "Approve and continue".');
        }
        const runnerTier = cfg.models.task_runners[name];
        const planned = tasks.get(task);
        if (runnerTier && planned && planned !== runnerTier) {
          const want = Object.keys(cfg.models.task_runners).find((n) => cfg.models.task_runners[n] === planned);
          return decide('deny', `${task} has Tier ${planned} in the approved plan. Run ${want ? `"${want}"` : `the ${planned} Task Runner`} instead, or go back to plan to change the Tier.`);
        }
        const session = sessionTier(cfg, run, scope, event);
        if (runnerTier && session && tierRank(cfg, runnerTier) > tierRank(cfg, session)) {
          return decide('deny', `"${name}" runs on the ${runnerTier} tier and this session runs on ${session}. VS Code does not start a `
            + `subagent above the session's model cost. Stop and show the card; the person can press "Continue on the ${runnerTier} model".`);
        }
        if (!dispatched.includes(task)) run.state.tasks_dispatched = [...dispatched, task];
        saveState(run);
      }
    }
  }

  usageOf(run, scope).tool_calls += 1;
  saveUsage(run);
}

function onPostTool(cfg, root, scope, event) {
  const runs = runsPath(root, cfg);
  const lock = acquireLock(runs);
  let syncNeeded = false;
  try {
    const run = loadRun(root, cfg);
    if (run.status !== 'ok') return;
    const tool = String(event.tool_name || '');
    const { isEdit, isTerminal } = toolKinds(cfg, tool);
    if (IMPLEMENT.has(scope) && (isEdit || isTerminal)) syncNeeded = true;
    // The orchestrator may have switched the active run for this chat.
    if (scope === 'orchestrator' && isEdit) bindSession(root, cfg, event.session_id, run.id);
    const resp = event.tool_response;
    const bytes = resp == null ? 0 : Buffer.byteLength(typeof resp === 'string' ? resp : JSON.stringify(resp));
    usageOf(run, scope).output_bytes += bytes;
    saveUsage(run);
  } finally {
    releaseLock(lock);
  }
  if (syncNeeded) {
    try { codegraph.backgroundSync(root, cfg.code_index); } catch { /* never block on the index */ }
  }
}

// Status snapshot before compaction goes to its own file, so it never changes an
// approved artifact.
function onPreCompact(cfg, root, scope) {
  const run = loadRun(root, cfg);
  if (run.status !== 'ok') return;
  const approvals = Object.keys(run.state.approvals).filter((k) => k !== 'tasks');
  const lines = [
    `## Status at compaction ${new Date().toISOString()}`,
    '',
    `- Active stage: ${run.state.stage} (${run.state.stage_status || 'working'}), agent scope: ${scope}`,
    `- Approved: ${approvals.join(', ') || 'none'}${run.state.approvals.tasks ? `; task reviews: ${run.state.approvals.tasks}` : ''}`,
    `- Tasks started: ${(run.state.tasks_dispatched || []).join(', ') || 'none'}`,
    '',
  ];
  fs.appendFileSync(path.join(run.dir, 'status.md'), lines.join('\n') + '\n');
}

// ---------- main ----------

function main() {
  const [mode, scope = ''] = process.argv.slice(2);
  let event = {};
  try {
    const raw = fs.readFileSync(0, 'utf8');
    if (raw.trim()) event = JSON.parse(raw);
  } catch { /* no stdin */ }
  if (!isPlainObject(event)) event = {};
  const cwd = path.resolve(event.cwd || process.cwd());
  const root = gitTop(cwd) || cwd;
  const cfg = loadConfig(root);
  if (mode === 'prompt') {
    let result;
    try {
      result = onPrompt(cfg, root, scope, event);
    } catch (err) {
      if (err instanceof Block) {
        process.stderr.write(err.message + '\n');
        process.exit(2);
      }
      throw err;
    }
    const notes = result.notes;
    if (result.saveReason) notes.push(codegraphSave(cfg, root, result.saveReason));
    if (notes.length) addPromptContext(notes.join(' '));
    return;
  }
  if (mode === 'pre-tool') return onPreTool(cfg, root, scope, event);
  if (mode === 'post-tool') return onPostTool(cfg, root, scope, event);
  if (mode === 'pre-compact') return onPreCompact(cfg, root, scope, event);
}

try {
  main();
} catch (err) {
  // A bug in the hook must not stop the agent: exit 1 is a non-blocking warning.
  process.stderr.write(`enterprise-ai-harness hook error: ${err && err.stack || err}\n`);
  process.exit(1);
}
