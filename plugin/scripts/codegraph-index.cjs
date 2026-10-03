#!/usr/bin/env node
// Code graph seed handling for change-harness.
//
// The working index lives in .codegraph/ (local; CodeGraph ignores it in git).
// A compressed snapshot of it, the seed, is committed in .codegraph-seed/ so a
// fresh clone restores the index instead of building it from scratch.
//
// Used by harness-hook.cjs; also runnable by hand:
//   node codegraph-index.cjs status|restore|sync|save [repo-root]
'use strict';

const fs = require('fs');
const path = require('path');
const zlib = require('zlib');
const cp = require('child_process');

const SEED_FILE = 'codegraph.db.gz';
const META_FILE = 'SEED.json';
const SYNC_DEBOUNCE_MS = 15000;
const LFS_WARN_BYTES = 50 * 1024 * 1024;
const CODEGRAPH_GITIGNORE = '# CodeGraph data files — local to each machine, not for committing.\n'
  + '# Ignore everything in .codegraph/ except this file itself, so transient\n'
  + '# files (the database, daemon.pid, sockets, logs) never show up in git.\n*\n!.gitignore\n';

function settings(idx) {
  return {
    dir: idx.dir || '.codegraph',
    file: idx.file || 'codegraph.db',
    sync: idx.sync || '',
    seedDir: idx.seed_dir || '.codegraph-seed',
    refresh: idx.seed_refresh || 'stale',
    maxAgeDays: Number(idx.seed_max_age_days || 7),
  };
}

function paths(root, idx) {
  const s = settings(idx);
  const dir = path.join(root, s.dir);
  const seedDir = path.join(root, s.seedDir);
  return { ...s, dirPath: dir, db: path.join(dir, s.file), seedPath: path.join(seedDir, SEED_FILE),
    metaPath: path.join(seedDir, META_FILE), seedDirPath: seedDir };
}

function readMeta(p) {
  try { return JSON.parse(fs.readFileSync(p.metaPath, 'utf8')); } catch { return null; }
}

function git(root, args) {
  try {
    return cp.execFileSync('git', args, { cwd: root, timeout: 5000, stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim();
  } catch { return ''; }
}

function status(root, idx) {
  const p = paths(root, idx);
  const meta = readMeta(p);
  const seedExists = fs.existsSync(p.seedPath);
  const ageDays = meta && meta.built_at ? (Date.now() - Date.parse(meta.built_at)) / 86400000 : null;
  return {
    index: fs.existsSync(p.db),
    seed: seedExists,
    seedCommit: meta ? meta.commit : null,
    seedAgeDays: ageDays == null ? null : Math.round(ageDays * 10) / 10,
    seedBytes: seedExists ? fs.statSync(p.seedPath).size : 0,
  };
}

// Restore .codegraph/ from the committed seed when the clone has no index yet.
// CodeGraph reconciles the restored index with the working tree on its next
// start (size, mtime and content hash), so only changed files are re-parsed.
function restore(root, idx) {
  const p = paths(root, idx);
  if (fs.existsSync(p.db) || !fs.existsSync(p.seedPath)) return null;
  fs.mkdirSync(p.dirPath, { recursive: true });
  const tmp = `${p.db}.restore-${process.pid}`;
  fs.writeFileSync(tmp, zlib.gunzipSync(fs.readFileSync(p.seedPath)));
  fs.renameSync(tmp, p.db);
  const gi = path.join(p.dirPath, '.gitignore');
  if (!fs.existsSync(gi)) fs.writeFileSync(gi, CODEGRAPH_GITIGNORE);
  const meta = readMeta(p);
  return `Code graph restored from the committed seed${meta && meta.commit ? ` (built at ${meta.commit.slice(0, 10)})` : ''}; `
    + 'the code graph server catches up with newer changes when it starts.';
}

function daemonAlive(p) {
  try {
    const raw = fs.readFileSync(path.join(p.dirPath, 'daemon.pid'), 'utf8').trim();
    let pid;
    try { pid = Number(JSON.parse(raw).pid); } catch { pid = Number(raw.split(/\s+/)[0]); }  // JSON, or old plain pid
    if (!pid) return false;
    process.kill(pid, 0);
    return true;
  } catch { return false; }
}

// After an agent edits code: if the code graph server is running, its file
// watcher already syncs; otherwise start a debounced background sync.
function backgroundSync(root, idx) {
  const p = paths(root, idx);
  if (!p.sync || !fs.existsSync(p.db)) return 'skipped';
  if (daemonAlive(p)) return 'watcher';
  const stamp = path.join(p.dirPath, '.harness-sync');
  try {
    if (Date.now() - fs.statSync(stamp).mtimeMs < SYNC_DEBOUNCE_MS) return 'debounced';
  } catch { /* first sync */ }
  fs.writeFileSync(stamp, new Date().toISOString() + '\n');
  const child = cp.spawn(p.sync, { cwd: root, shell: true, detached: true, stdio: 'ignore',
    env: { ...process.env, DO_NOT_TRACK: '1' } });
  child.unref();
  return 'started';
}

function snapshot(src, dest) {
  try {
    process.removeAllListeners('warning');
    const { DatabaseSync } = require('node:sqlite');
    const db = new DatabaseSync(src);
    try { db.exec(`VACUUM INTO '${dest.replace(/'/g, "''")}'`); } finally { db.close(); }
    return;
  } catch (err) {
    if (err && err.code !== 'ERR_UNKNOWN_BUILTIN_MODULE' && !/No such built-in module/.test(String(err))) throw err;
  }
  // Older Node without node:sqlite: copy only when there is no pending write-ahead log.
  const wal = `${src}-wal`;
  if (fs.existsSync(wal) && fs.statSync(wal).size > 0) {
    throw new Error('Node has no node:sqlite and the index has unsaved changes; stop the code graph server and try again, or use Node 22.5+.');
  }
  fs.copyFileSync(src, dest);
}

// Write .codegraph-seed/ from the working index: sync, consistent snapshot, gzip.
function save(root, idx, reason) {
  const p = paths(root, idx);
  if (!fs.existsSync(p.db)) throw new Error(`No code graph index at ${path.relative(root, p.db)}; build it first.`);
  if (p.sync) {
    try {
      cp.execSync(p.sync, { cwd: root, timeout: 90000, stdio: 'ignore', env: { ...process.env, DO_NOT_TRACK: '1' } });
    } catch { /* a failed sync still leaves a usable, slightly older index */ }
  }
  fs.mkdirSync(p.seedDirPath, { recursive: true });
  const tmp = path.join(p.seedDirPath, `.snapshot-${process.pid}.db`);
  try {
    snapshot(p.db, tmp);
    const gz = zlib.gzipSync(fs.readFileSync(tmp), { level: 9 });
    fs.writeFileSync(p.seedPath, gz);
    const meta = {
      commit: git(root, ['rev-parse', 'HEAD']) || null,
      built_at: new Date().toISOString(),
      reason: reason || 'manual',
      index_bytes: fs.statSync(tmp).size,
      seed_bytes: gz.length,
    };
    fs.writeFileSync(p.metaPath, JSON.stringify(meta, null, 2) + '\n');
    let note = `Code graph seed saved to ${path.relative(root, p.seedDirPath)}/ (${Math.round(gz.length / 1024)} KB).`;
    if (gz.length > LFS_WARN_BYTES) note += ' It is over 50 MB: track it with Git LFS before committing.';
    return note;
  } finally {
    try { fs.unlinkSync(tmp); } catch { /* ignore */ }
  }
}

// Should the Reviewer refresh the committed seed on verify approval?
function shouldRefresh(root, idx) {
  const p = paths(root, idx);
  if (!fs.existsSync(p.db)) return false;
  if (p.refresh === 'manual') return false;
  if (p.refresh === 'every-pr') return true;
  const st = status(root, idx);
  return !st.seed || st.seedAgeDays == null || st.seedAgeDays >= p.maxAgeDays;
}

module.exports = { status, restore, backgroundSync, save, shouldRefresh, paths };

if (require.main === module) {
  const [cmd = 'status', rootArg] = process.argv.slice(2);
  const root = path.resolve(rootArg || process.cwd());
  let idx = {};
  try { idx = JSON.parse(fs.readFileSync(path.join(__dirname, 'config.json'), 'utf8')).code_index || {}; } catch { /* defaults */ }
  try {
    if (cmd === 'status') console.log(JSON.stringify(status(root, idx), null, 2));
    else if (cmd === 'restore') console.log(restore(root, idx) || 'nothing to restore');
    else if (cmd === 'sync') console.log(backgroundSync(root, idx));
    else if (cmd === 'save') console.log(save(root, idx, 'manual'));
    else { console.error(`unknown command ${cmd}`); process.exit(2); }
  } catch (err) {
    console.error(err.message || err);
    process.exit(1);
  }
}
