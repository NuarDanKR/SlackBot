/**
 * Log storage with injected filesystem, paths and date policy.
 * Factory creation performs no I/O. Only append swallows storage failures;
 * rendering writes retain their original error and temporary-file behavior.
 */
export function createLogStore({
  fs, path, LOG_ENABLED, LOG_RAW_DIR, LOG_DIR, localParts, console,
}) {
  /**
   * 한 건 기록. **실패해도 절대 던지지 않는다** — 답변은 이미 나갔거나 나가는 중이고,
   * 로그가 본업을 죽이면 안 된다. 대신 콘솔에 남겨 journalctl 에서 보이게 한다.
   *
   * @param {object} entry kind 는 qa|empty|daily|weekly|health|ingest
   */
  function append(entry) {
    if (!LOG_ENABLED) return;
    try {
      const at = entry.at || new Date().toISOString();
      const { ym } = localParts(at);
      fs.mkdirSync(LOG_RAW_DIR, { recursive: true });
      fs.appendFileSync(
        path.join(LOG_RAW_DIR, `qa-${ym}.jsonl`),
        JSON.stringify({ ...entry, at }) + '\n',
        'utf8',
      );
    } catch (err) {
      console.error('[log] 대화 기록 실패 —', err.message);
    }
  }

  /* ── ② 렌더 ───────────────────────────────────────────────────── */

  /** 그 달 원본을 읽는다. 깨진 줄은 세어서 md 헤더에 드러낸다 (조용히 버리지 않는다). */
  function readRaw(ym) {
    const file = path.join(LOG_RAW_DIR, `qa-${ym}.jsonl`);
    if (!fs.existsSync(file)) return { entries: [], broken: 0 };
    const entries = [];
    let broken = 0;
    for (const line of fs.readFileSync(file, 'utf8').split('\n')) {
      const t = line.trim();
      if (!t) continue;
      try {
        entries.push(JSON.parse(t));
      } catch {
        broken += 1;
      }
    }
    return { entries, broken };
  }

  /** 원본이 있는 달 목록 (오름차순) */
  function months() {
    if (!fs.existsSync(LOG_RAW_DIR)) return [];
    return fs
      .readdirSync(LOG_RAW_DIR)
      .map((f) => /^qa-(\d{4}-\d{2})\.jsonl$/.exec(f)?.[1])
      .filter(Boolean)
      .sort();
  }

  /** 이미 렌더돼 있는 달. 원본이 사라졌는지 가릴 때 쓴다. */
  function renderedMonths() {
    if (!fs.existsSync(LOG_DIR)) return [];
    return fs
      .readdirSync(LOG_DIR)
      .map((f) => /^(\d{4}-\d{2})\.md$/.exec(f)?.[1])
      .filter(Boolean)
      .sort();
  }

  /**
   * 원자적 쓰기. 내용이 같으면 쓰지 않는다 — 헛커밋을 만들지 않으려고.
   * write=false 면 바뀌는지만 보고 파일은 건드리지 않는다 (미리보기용).
   */
  function writeIfChanged(file, text, write) {
    const same = fs.existsSync(file) && fs.readFileSync(file, 'utf8') === text;
    if (same || !write) return !same;
    fs.mkdirSync(path.dirname(file), { recursive: true });
    const tmp = `${file}.tmp`;
    fs.writeFileSync(tmp, text, 'utf8');
    fs.renameSync(tmp, file);
    return true;
  }
  return { append, readRaw, months, renderedMonths, writeIfChanged };
}
