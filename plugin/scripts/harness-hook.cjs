#!/usr/bin/env node
// change-harness hook for the VS Code Local agent harness.
//
// Wired as agent-scoped hooks in each harness agent's frontmatter (shipped in
// the plugin), so it only runs while a harness agent is active:
//   node "${PLUGIN_ROOT}/scripts/harness-hook.cjs" <prompt|pre-tool|post-tool|pre-compact> <scope>
//
// It owns .changes/<run>/state.json (stage, approvals) and .changes/<run>/.usage.json
// (tool-call and output budgets), keeps run files out of git with .git/info/exclude,
// and keeps the code graph current (restore from seed, sync after edits, refresh
// the committed seed). Agents read these files but cannot edit them.
'use strict';

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const cp = require('child_process');
const os = require('os');
const codegraph = require('./codegraph-index.cjs');

const HOOK_DIR = __dirname;
const PRE_IMPLEMENT = new Set(['orchestrator', 'check', 'intake', 'grill', 'impact', 'plan', 'scout', 'cross-check']);
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
  // Personal settings: ~/.copilot/<plugin>.local.yaml, then harness.local.yaml in the repo.
  const personal = [path.join(os.homedir(), '.copilot', `${cfg.plugin || 'change-harness'}.local.yaml`),
    path.join(root, 'harness.local.yaml')];
  for (const localFile of personal) {
    if (!fs.existsSync(localFile)) continue;
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
  cfg.runs = cfg.runs || {};
  cfg.code_index = cfg.code_index || {};
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
      stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim();
  } catch { return; }
  if (!rel) return;
  const file = path.isAbsolute(rel) ? rel : path.join(root, rel);
  const wanted = cfg.runs.commit
    ? ['.changes/current', '.changes/.hooks-ok', '.changes/.checked', '.changes/.audit.log', '.changes/**/.usage.json']
    : ['.changes/'];
  // CodeGraph's own .codegraph/.gitignore re-includes itself; excluding the
  // directory keeps the working index entirely out of git status.
  wanted.push(`${(cfg.code_index.dir || '.codegraph').replace(/\/+$/, '')}/`, 'harness.local.yaml');
  let text = '';
  try { text = fs.readFileSync(file, 'utf8'); } catch { /* new file */ }
  const lines = text.split(/\r?\n/);
  const missing = wanted.filter((w) => !lines.includes(w));
  if (!missing.length) return;
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const header = lines.includes('# change-harness') ? [] : ['# change-harness'];
  fs.appendFileSync(file, (text && !text.endsWith('\n') ? '\n' : '') + [...header, ...missing].join('\n') + '\n');
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

// ---------- model tiers and cross-checks ----------

function tierRank(cfg, tier) { return cfg.models.order.indexOf(tier); }

function modelOf(cfg, tier) {
  const m = (cfg.models.first_model || {})[tier];
  return m ? ` (${m})` : '';
}

// The tier of this agent session: what the person chose with a "Continue on the
// <tier> model" button in this session, else the agent's configured tier.
function sessionTier(cfg, run, scope, event) {
  const rec = (run.state.session_tiers || {})[scope];
  if (rec && (!rec.session || !event.session_id || rec.session === event.session_id)) return rec.tier;
  return cfg.models.scope_tiers[scope] || null;
}

// Tier per task from plan.md: "### T3. <title>" followed by "- Tier: deep".
function planTaskTiers(run) {
  const tiers = {};
  let text;
  try { text = fs.readFileSync(path.join(run.dir, 'plan.md'), 'utf8'); } catch { return tiers; }
  let task = null;
  for (const line of text.split(/\r?\n/)) {
    const h = line.match(/^#{2,4}\s+(T\d+)\b/);
    if (h) { task = h[1]; continue; }
    if (/^#{1,4}\s/.test(line)) { task = null; continue; }
    const t = task && line.match(/^\s*[-*]\s*Tier:\s*([\w-]+)/i);
    if (t) tiers[task] = t[1].toLowerCase();
  }
  return tiers;
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
      blockPrompt(`Nothing to cross-check yet: ${name} is missing or empty in .changes/${run.id}/.`);
    }
    files[name] = sha256(file);
  }
  const spec = cfg.models.cross_check[stage] || {};
  run.state.cross_checks = run.state.cross_checks || {};
  run.state.cross_checks[stage] = { at: new Date().toISOString(), checker: spec.checker || null, checked: files };
  run.usage['cross-check'] = { tool_calls: 0, output_bytes: 0 };
  saveState(run); saveUsage(run);
  const names = Object.keys(files).join(', ');
  return `Cross-check of ${stage} started on the ${spec.checker || 'chosen'} tier; ${names} hashed as checked. `
    + `Write the findings to .changes/${run.id}/${stage}-check.md, starting with a "Verdict:" line.`;
}

// Returns the cross-check record for an approval, or blocks the approval when a
// required check has not run and the person did not say "skip cross-check".
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
    blockPrompt(`The ${stage} needs a cross-check on the ${spec.checker} tier before approval. `
      + `Press "Cross-check with the ${spec.checker} model", or approve with "skip cross-check" to record that you skipped it.`);
  }
  return skip ? { skipped: true, by, required: !!spec.required } : null;
}

function describeCheck(stage, cc) {
  if (!cc) return '';
  if (cc.skipped) return ` Cross-check of ${stage} skipped by ${cc.by}; recorded.`;
  return ` Cross-check verdict: ${cc.verdict}${cc.changed_after_check ? `; ${stage} changed after the check` : ''}.`;
}

function statusLine(cfg, run, scope) {
  const key = scope || run.state.stage;
  const used = usageOf(run, key).tool_calls;
  const b = budgetFor(cfg, run, key);
  const review = cfg.review[stageOf(key)] || 'required';
  let line = `Harness status: run ${run.id}, active stage ${run.state.stage}, this agent's scope ${key}, `
    + `${used} of ${b.calls || 'unlimited'} tool calls used, review mode ${review}.`;
  const tier = sessionTier(cfg, run, key, {});
  if (tier) line += ` Model tier: ${tier}${modelOf(cfg, tier)}.`;
  const spec = cfg.models.cross_check[key];
  if (spec && spec.required && !(run.state.cross_checks || {})[key]) {
    line += ` A cross-check on the ${spec.checker} tier is required before ${key} can be approved.`;
  }
  return line;
}

// ---------- prompt: approvals, revisions, stop, extend ----------

function parsePrompt(text, scope) {
  const t = text || '';
  let m;
  const skip = /\bskip(ping)? (the )?cross-?check\b/i.test(t);
  if ((m = t.match(/\[harness:cross-check\s+([\w-]+)\]/i))) return { action: 'cross-check', stage: m[1].toLowerCase() };
  if ((m = t.match(/\[harness:tier\s+([\w-]+)\]/i))) return { action: 'tier', tier: m[1].toLowerCase() };
  if ((m = t.match(/\[harness:approve\s+([\w-]+)\]/i))) return { action: 'approve', stage: m[1].toLowerCase(), skip };
  if ((m = t.match(/\[harness:revise\s+([\w-]+)\]/i))) return { action: 'revise', stage: m[1].toLowerCase() };
  if (/\[harness:stop\]/i.test(t)) return { action: 'stop' };
  if (/\[harness:extend\]/i.test(t) || /^\s*extend\b/i.test(t)) return { action: 'extend' };
  if (/\[harness:codegraph-save\]/i.test(t) || /^\s*(save|refresh) (the )?code ?graph\b/i.test(t)) return { action: 'codegraph-save' };
  if ((m = t.match(/^\s*back to\s+([\w-]+)/i))) return { action: 'revise', stage: m[1].toLowerCase() };
  const userStages = ['intake', 'grill', 'impact', 'plan', 'implement', 'verify'];
  if (/^\s*approve\s*[.!]?\s*$/i.test(t) || (skip && /^\s*approve\b/i.test(t))) {
    if (scope === 'implement') return { action: 'approve', stage: 'task' };
    if (userStages.includes(scope)) return { action: 'approve', stage: scope, skip };
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
    // A check of a later stage looked at artifacts built on what is being revised.
    if (i > idx && run.state.cross_checks) delete run.state.cross_checks[s];
  });
  if (idx < impl || (inclusive && idx === impl)) delete run.state.approvals.tasks;
}

function approve(cfg, run, stage, root, skip) {
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
  if (next !== 'done') run.usage[next] = { tool_calls: 0, output_bytes: 0 };
  saveState(run); saveUsage(run);
  return `Approved ${stage} by ${by}; artifact hashes recorded.${describeCheck(stage, cc)} Active stage is now ${next}.`;
}

function revise(cfg, run, stage) {
  if (!cfg.stages.includes(stage)) blockPrompt(`Unknown stage "${stage}". Stages are: ${cfg.stages.join(', ')}.`);
  clearLaterApprovals(cfg, run, stage, true);
  run.state.stage = stage;
  run.state.stage_status = 'working';
  saveState(run);
  return `Revising ${stage}. Approvals for ${stage} and later stages were cleared and will be needed again.`;
}

function codegraphSave(cfg, root, reason) {
  try { return codegraph.save(root, cfg.code_index, reason); } catch (err) { return `Code graph seed not saved: ${err.message || err}`; }
}

function onPrompt(cfg, root, scope, event) {
  const notes = [];
  try {
    fs.mkdirSync(path.join(root, '.changes'), { recursive: true });
    fs.writeFileSync(path.join(root, '.changes', '.hooks-ok'), new Date().toISOString().slice(0, 10) + '\n');
    ensureLocalExcludes(root, cfg);
  } catch { /* read-only checkout */ }
  try {
    const restored = codegraph.restore(root, cfg.code_index);
    if (restored) notes.push(restored);
  } catch (err) { notes.push(`Code graph seed could not be restored: ${err.message || err}`); }
  const cmd = parsePrompt(event.prompt, scope);
  if (cmd && cmd.action === 'codegraph-save') notes.push(codegraphSave(cfg, root, 'requested'));
  const run = loadRun(root);
  if (!run) {
    if (notes.length) addPromptContext(notes.join(' '));
    return;
  }
  if (cmd) {
    if (cmd.action === 'cross-check') notes.push(startCrossCheck(cfg, run, cmd.stage));
    else if (cmd.action === 'tier') {
      if (!cfg.models.order.includes(cmd.tier)) blockPrompt(`Unknown model tier "${cmd.tier}". Tiers: ${cfg.models.order.join(', ') || 'none configured'}.`);
      run.state.session_tiers = run.state.session_tiers || {};
      run.state.session_tiers[scope] = { tier: cmd.tier, session: event.session_id || null, at: new Date().toISOString() };
      saveState(run);
      notes.push(`This ${scope} session now runs on the ${cmd.tier} tier${modelOf(cfg, cmd.tier)}. Task Runners up to ${cmd.tier} can run.`);
    }
    else if (cmd.action === 'approve') {
      notes.push(approve(cfg, run, cmd.stage, root, cmd.skip));
      if (cmd.stage === 'verify' && codegraph.shouldRefresh(root, cfg.code_index)) {
        notes.push(codegraphSave(cfg, root, `pr ${run.id}`)
          + ' Commit .codegraph-seed/ with the PR as its own commit, "Refresh code graph seed".');
      }
    }
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
      const cgDir = (cfg.code_index.dir || '.codegraph') + '/';
      const seedDir = (cfg.code_index.seed_dir || '.codegraph-seed') + '/';
      if (p.startsWith(cgDir) || p.startsWith(seedDir)) {
        return decide('deny', `${p} is maintained by the harness hook and the code graph server. Say "save code graph" to refresh the seed.`);
      }
      if (p.startsWith(runRel) && /^[\w-]+-check\.md$/.test(p.slice(runRel.length)) && scope !== 'cross-check') {
        return decide('deny', `${p} is written by the Checker on the stronger model. Record what you did with its findings in the stage's own artifact.`);
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

  // Task Runner dispatch: the runner's tier must match the task's Tier in the approved
  // plan, and must not be above this session's tier (VS Code would refuse it anyway).
  if (isSubagent && scope === 'implement') {
    const name = String(input.agentName || input.agent_name || input.agent || '');
    const runnerTier = cfg.models.task_runners[name];
    if (runnerTier) {
      const task = (String(input.prompt || input.description || '').match(/\bT\d+\b/) || [])[0];
      const planned = task ? planTaskTiers(run)[task] : null;
      if (planned && planned !== runnerTier) {
        const want = Object.keys(cfg.models.task_runners).find((n) => cfg.models.task_runners[n] === planned);
        return decide('deny', `${task} has Tier ${planned} in the approved plan. Run ${want ? `"${want}"` : `the ${planned} Task Runner`} instead, or go back to plan to change the Tier.`);
      }
      const session = sessionTier(cfg, run, scope, event);
      if (session && tierRank(cfg, runnerTier) > tierRank(cfg, session)) {
        return decide('deny', `"${name}" runs on the ${runnerTier} tier and this session runs on ${session}. VS Code does not start a `
          + `subagent above the session's model cost. Stop and show the card; the person can press "Continue on the ${runnerTier} model".`);
      }
    }
    run.usage['implement-task'] = { tool_calls: 0, output_bytes: 0 };  // fresh per-task budget
  }

  for (const key of keys) usageOf(run, key).tool_calls += 1;
  saveUsage(run);
}

function onPostTool(cfg, root, scope, event) {
  const run = loadRun(root);
  if (!run) return;
  if (IMPLEMENT.has(scope)) {
    const tool = String(event.tool_name || '');
    const changesFiles = new RegExp(cfg.hooks.edit_tools || '(edit|create|write|replace|insert|patch|delete|rename|move)', 'i').test(tool)
      || new RegExp(cfg.hooks.terminal_tools || '(terminal|runInTerminal|run_in_terminal|execute|command)', 'i').test(tool);
    if (changesFiles) {
      try { codegraph.backgroundSync(root, cfg.code_index); } catch { /* never block on the index */ }
    }
  }
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
