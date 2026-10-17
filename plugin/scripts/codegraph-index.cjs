#!/usr/bin/env node
// Code graph seed handling for enterprise-ai-harness.
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
const SQLITE_HEADER = Buffer.from('SQLite format 3\0', 'latin1');
const LFS_WARN_BYTES = 50 * 1024 * 1024;
const SYNC_TIMEOUT_MS = 10 * 60 * 1000;
const SAVE_SYNC_TIMEOUT_MS = 60 * 1000;
const MAX_SYNC_ROUNDS = 5;
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
    metaPath: path.join(seedDir, META_FILE), seedDirPath: seedDir,
    syncPid: path.join(dir, '.harness-sync.pid'), syncDirty: path.join(dir, '.harness-sync-dirty'),
    restoreFailed: path.join(dir, '.harness-restore-failed') };
}

function readMeta(p) {
  try { return JSON.parse(fs.readFileSync(p.metaPath, 'utf8')); } catch { return null; }
}

function git(root, args) {
  try {
    return cp.execFileSync('git', args, { cwd: root, timeout: 5000, stdio: ['ignore', 'pipe', 'ignore'], windowsHide: true })
      .toString().trim();
  } catch { return ''; }
}

function pidAlive(pid) {
  if (!pid) return false;
  try { process.kill(pid, 0); return true; } catch (err) { return err.code === 'EPERM'; }
}

function readPid(file) {
  try {
    const raw = fs.readFileSync(file, 'utf8').trim();
    try { return Number(JSON.parse(raw).pid); } catch { return Number(raw.split(/\s+/)[0]); }  // JSON, or old plain pid
  } catch { return 0; }
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
    syncRunning: pidAlive(readPid(p.syncPid)),
    serverRunning: daemonAlive(p),
  };
}

function seedFingerprint(p) {
  const st = fs.statSync(p.seedPath);
  return `${st.size}:${st.mtimeMs}`;
}

// Restore .codegraph/ from the committed seed when the clone has no index yet.
// CodeGraph stores paths relative to the repo and compares size, time and content
// hash on its next start, so only files changed since the seed are re-parsed.
function restore(root, idx) {
  const p = paths(root, idx);
  if (fs.existsSync(p.db) || !fs.existsSync(p.seedPath)) return null;
  const fingerprint = seedFingerprint(p);
  try {
    if (fs.readFileSync(p.restoreFailed, 'utf8').trim() === fingerprint) return null;  // already reported for this seed
  } catch { /* no earlier failure */ }
  fs.mkdirSync(p.dirPath, { recursive: true });
  const fail = (why) => {
    try { fs.writeFileSync(p.restoreFailed, fingerprint + '\n'); } catch { /* ignore */ }
    return `Code graph seed not restored: ${why}. The code graph server builds the index on its first run instead.`;
  };
  const raw = fs.readFileSync(p.seedPath);
  if (raw.subarray(0, 40).toString('latin1').startsWith('version https://git-lfs')) {
    return fail('.codegraph-seed/codegraph.db.gz is a Git LFS pointer; run "git lfs pull" first');
  }
  let data;
  try { data = zlib.gunzipSync(raw); } catch (err) { return fail(`the seed is not a valid gzip file (${err.code || err.message})`); }
  if (!data.subarray(0, SQLITE_HEADER.length).equals(SQLITE_HEADER)) return fail('the seed does not contain a SQLite database');
  const meta = readMeta(p);
  if (meta && meta.index_bytes && meta.index_bytes !== data.length) {
    return fail(`the seed holds ${data.length} bytes but SEED.json expects ${meta.index_bytes}`);
  }
  const tmp = `${p.db}.restore-${process.pid}`;
  fs.writeFileSync(tmp, data);
  fs.renameSync(tmp, p.db);
  try { fs.unlinkSync(p.restoreFailed); } catch { /* none */ }
  const gi = path.join(p.dirPath, '.gitignore');
  if (!fs.existsSync(gi)) fs.writeFileSync(gi, CODEGRAPH_GITIGNORE);
  return `Code graph restored from the committed seed${meta && meta.commit ? ` (built at ${meta.commit.slice(0, 10)})` : ''}; `
    + 'the code graph server catches up with newer changes when it starts.';
}

function daemonAlive(p) {
  return pidAlive(readPid(path.join(p.dirPath, 'daemon.pid')));
}

// After an agent edits code: if the CodeGraph server is running, its file watcher
// already syncs. Otherwise one background sync loop runs at a time; edits made while
// it runs set a dirty flag, and the loop syncs again before it exits.
function backgroundSync(root, idx) {
  const p = paths(root, idx);
  if (!p.sync || !fs.existsSync(p.db)) return 'skipped';
  if (daemonAlive(p)) return 'watcher';
  fs.writeFileSync(p.syncDirty, new Date().toISOString() + '\n');
  if (pidAlive(readPid(p.syncPid))) return 'queued';
  const child = cp.spawn(process.execPath, [__filename, 'sync-loop', root], {
    cwd: root, detached: true, stdio: 'ignore', windowsHide: true,
    env: { ...process.env, DO_NOT_TRACK: '1', HARNESS_CODE_INDEX: JSON.stringify(idx) },
  });
  try { fs.writeFileSync(p.syncPid, JSON.stringify({ pid: child.pid, at: new Date().toISOString() }) + '\n'); } catch { /* ignore */ }
  child.unref();
  return 'started';
}

function syncLoop(root, idx) {
  const p = paths(root, idx);
  fs.writeFileSync(p.syncPid, JSON.stringify({ pid: process.pid, at: new Date().toISOString() }) + '\n');
  try {
    for (let round = 0; round < MAX_SYNC_ROUNDS && fs.existsSync(p.syncDirty); round++) {
      try { fs.unlinkSync(p.syncDirty); } catch { /* raced */ }
      try {
        cp.execSync(p.sync, { cwd: root, timeout: SYNC_TIMEOUT_MS, stdio: 'ignore', windowsHide: true,
          env: { ...process.env, DO_NOT_TRACK: '1' } });
      } catch { /* a failed sync leaves the older index; the next edit tries again */ }
    }
  } finally {
    if (readPid(p.syncPid) === process.pid) { try { fs.unlinkSync(p.syncPid); } catch { /* ignore */ } }
  }
}

function sqlite3Cli() {
  try {
    cp.execFileSync('sqlite3', ['-version'], { stdio: 'ignore', timeout: 5000, windowsHide: true });
    return true;
  } catch { return false; }
}

// A consistent copy of a WAL-mode database, even while CodeGraph is writing to it.
function snapshot(src, dest) {
  let sqlite = null;
  try {
    process.removeAllListeners('warning');
    sqlite = require('node:sqlite');
  } catch { /* Node without node:sqlite (before 22.13 without a flag) */ }
  const q = dest.replace(/'/g, "''");
  if (sqlite && sqlite.DatabaseSync) {
    const db = new sqlite.DatabaseSync(src);
    try { db.exec(`VACUUM INTO '${q}'`); } finally { db.close(); }
    return;
  }
  if (sqlite3Cli()) {
    cp.execFileSync('sqlite3', [src, `VACUUM INTO '${q}'`], { stdio: 'ignore', timeout: 120000, windowsHide: true });
    return;
  }
  const wal = `${src}-wal`;
  if (fs.existsSync(wal) && fs.statSync(wal).size > 0) {
    throw new Error('saving the seed needs Node.js 22.13 or later (node:sqlite) or the sqlite3 command, because the index has unsaved changes');
  }
  fs.copyFileSync(src, dest);
}

function cleanTemp(dir) {
  try {
    for (const f of fs.readdirSync(dir)) {
      if (/^\.(snapshot|seed)-\d+/.test(f)) {
        const full = path.join(dir, f);
        if (Date.now() - fs.statSync(full).mtimeMs > 3600 * 1000) fs.unlinkSync(full);
      }
    }
  } catch { /* ignore */ }
}

// Write .codegraph-seed/ from the working index: catch up, consistent snapshot, gzip,
// and an atomic rename into place. Temporary files stay in .codegraph/ (ignored by git).
function save(root, idx, reason) {
  const p = paths(root, idx);
  if (!fs.existsSync(p.db)) throw new Error(`No code graph index at ${path.relative(root, p.db)}; build it first.`);
  cleanTemp(p.dirPath);
  if (p.sync && !daemonAlive(p) && !pidAlive(readPid(p.syncPid))) {
    try {
      cp.execSync(p.sync, { cwd: root, timeout: SAVE_SYNC_TIMEOUT_MS, stdio: 'ignore', windowsHide: true,
        env: { ...process.env, DO_NOT_TRACK: '1' } });
    } catch { /* a failed sync still leaves a usable, slightly older index */ }
  }
  fs.mkdirSync(p.seedDirPath, { recursive: true });
  const snap = path.join(p.dirPath, `.snapshot-${process.pid}.db`);
  const gzTmp = path.join(p.dirPath, `.seed-${process.pid}.gz`);
  try {
    snapshot(p.db, snap);
    const data = fs.readFileSync(snap);
    const gz = zlib.gzipSync(data, { level: 6 });
    fs.writeFileSync(gzTmp, gz);
    fs.renameSync(gzTmp, p.seedPath);
    const meta = {
      commit: git(root, ['rev-parse', 'HEAD']) || null,
      built_at: new Date().toISOString(),
      reason: reason || 'manual',
      index_bytes: data.length,
      seed_bytes: gz.length,
    };
    fs.writeFileSync(p.metaPath, JSON.stringify(meta, null, 2) + '\n');
    try { fs.unlinkSync(p.restoreFailed); } catch { /* none */ }
    let note = `Code graph seed saved to ${path.relative(root, p.seedDirPath)}/ (${Math.round(gz.length / 1024)} KB).`;
    if (gz.length > LFS_WARN_BYTES) note += ' It is over 50 MB: track it with Git LFS before committing.';
    return note;
  } finally {
    for (const f of [snap, gzTmp]) { try { fs.unlinkSync(f); } catch { /* ignore */ } }
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
  try { idx = JSON.parse(process.env.HARNESS_CODE_INDEX || 'null') || {}; } catch { idx = {}; }
  if (!Object.keys(idx).length) {
    try { idx = JSON.parse(fs.readFileSync(path.join(__dirname, 'config.json'), 'utf8')).code_index || {}; } catch { /* defaults */ }
  }
  try {
    if (cmd === 'status') console.log(JSON.stringify(status(root, idx), null, 2));
    else if (cmd === 'restore') console.log(restore(root, idx) || 'nothing to restore');
    else if (cmd === 'sync') console.log(backgroundSync(root, idx));
    else if (cmd === 'sync-loop') syncLoop(root, idx);
    else if (cmd === 'save') console.log(save(root, idx, 'manual'));
    else { console.error(`unknown command ${cmd}`); process.exit(2); }
  } catch (err) {
    console.error(err.message || err);
    process.exit(1);
  }
}
