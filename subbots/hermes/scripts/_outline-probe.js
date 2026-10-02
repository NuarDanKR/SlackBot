/**
 * fixture 한 장을 임시 문서 아카이브에 놓고 readDocument 를 부른다.
 * 실제 아카이브를 안 건드리려고 HERMES_DOCS_DIR 을 돌려 별도 프로세스에서 돈다
 * (check-excel-sheets.js 와 같은 방식).
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));

export async function readDocumentInTmp(fixture, args) {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-outline-'));
  try {
    const proj = path.join(tmp, 'projects', args.project);
    fs.mkdirSync(proj, { recursive: true });
    fs.writeFileSync(path.join(proj, `20260805-시험-산정내역.md`), fixture, 'utf8');
    fs.writeFileSync(path.join(tmp, 'index.md'), '# 문서 아카이브\n', 'utf8');

    // Windows: 절대경로를 그대로 import specifier 로 주면 ERR_UNSUPPORTED_ESM_URL_SCHEME.
    const probe = `
      process.env.HERMES_DOCS_DIR = ${JSON.stringify(tmp)};
      const { readDocument } = await import(${JSON.stringify(
        pathToFileURL(path.join(here, '..', 'src', 'documents.js')).href,
      )});
      const { FULL_ACCESS } = await import(${JSON.stringify(
        pathToFileURL(path.join(here, '..', 'src', 'config.js')).href,
      )});
      console.log(JSON.stringify(readDocument({ ...${JSON.stringify(args)}, access: FULL_ACCESS })));
    `;
    const r = spawnSync(process.execPath, ['--input-type=module', '-e', probe], { encoding: 'utf-8' });
    if (r.status !== 0) return { error: (r.stderr || '').trim().split('\n')[0] };
    return JSON.parse(r.stdout.trim().split('\n').pop());
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
}
