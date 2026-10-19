#!/usr/bin/env python3
"""Prepare explicit HWP recovery generations. Never publish or index them."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from tybot import attachment_review
from tybot.archive.attachment_doc import revision_for
from tybot.archive.file_reconversion import reconvert
from tybot.archive.file_recovery import _safe_path
from tybot.archiving_bot import _instance_lock_name
from tybot.envfile import load_env_file
from tybot.lock import instance_lock


def prepare(root: Path, workspace: str, channel: str, file_id: str, *, apply=False, actor=''):
    if (not root.is_absolute() or not root.is_dir()
            or not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', workspace)
            or not re.fullmatch(r'C[A-Z0-9]+', channel)
            or not re.fullmatch(r'F[A-Z0-9]+', file_id)):
        raise ValueError('explicit archive/workspace/channel/file scope required')
    if apply and not actor.strip():
        raise ValueError('accountable actor required')
    items = [item for item in attachment_review.scan(root)
             if (item.workspace, item.channel_id, item.file_id) == (workspace, channel, file_id)]
    if len(items) != 1:
        raise ValueError('missing or ambiguous original staging metadata')
    item, = items
    meta_path = _safe_path(root.parent, item.meta_path)
    meta = json.loads(meta_path.read_text(encoding='utf-8'))
    if (meta.get('slack_file_id') != file_id or meta.get('filetype') != 'hwp'
            or meta.get('original_state') != 'retained'
            or meta.get('conversion_state') == 'blocked' or meta.get('status') == 'pii_refused'
            or meta.get('screen_result') == 'blocked'):
        raise PermissionError('a retained, non-blocked HWP is required')
    original = _safe_path(root.parent, Path(meta.get('object_path') or ''))
    legacy = root.parent / 'objects' / 'workspaces' / workspace / 'channels' / channel / 'attachments' / file_id
    current = meta_path.parents[2] / 'objects' / file_id
    if not original.is_absolute() or not original.is_file() or not any(
        original.resolve().is_relative_to(base.resolve()) for base in (legacy, current)
    ):
        raise ValueError('original escaped its file scope')
    result = dict(workspace=workspace, channel_id=channel, file_id=file_id,
                  read_only=not apply, published=False, indexed=False)
    if not apply:
        return result | {'status': 'hwp-render-planned'}
    # Called only with the collector lock held by main. This destination is
    # private staging and has no receipt pointer or searchable publication.
    destination = _safe_path(root.parent, meta_path.parent / 'reconversions')
    path = reconvert(meta, destination, render_hwp=True, actor=actor)
    updated = json.loads(path.read_text(encoding='utf-8'))
    revision = revision_for(sha256=updated['sha256'], converter_name=updated['converter_name'],
                            converter_version=updated['converter_version'], config=updated['converter_config'])
    return result | dict(
        status='blocked' if updated['conversion_state'] == 'blocked' else 'prepared-not-published',
        conversion_state=updated['conversion_state'], revision=revision,
        coverage_total=updated.get('coverage_total'), coverage_converted=updated.get('coverage_converted'),
        missing_units=updated.get('missing_units'), page_basis=updated['page_basis'],
        rendered_sha256=updated['rendered_sha256'], metadata_path=str(path),
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--channel', required=True)
    parser.add_argument('--file-id', action='append', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--by', default='')
    args = parser.parse_args(argv)
    if args.apply and not args.by.strip():
        parser.error('--apply requires --by')
    if len(args.file_id) > 10:
        parser.error('at most 10 explicit file IDs; no bulk mode')
    lock = None
    try:
        load_env_file()
        root = Path(os.environ['ARCHIVE_DIR'])
        if args.apply:
            lock = instance_lock(_instance_lock_name(args.workspace))
            lock.acquire()
        results = [prepare(root, args.workspace, args.channel, file_id,
                           apply=args.apply, actor=args.by) for file_id in dict.fromkeys(args.file_id)]
        print(json.dumps({'results': results}, ensure_ascii=True))
        return 2 if any(r['status'] == 'blocked' for r in results) else 0
    except Exception as exc:  # noqa: BLE001 - no stderr, filenames, document body or DSN
        print(json.dumps({'error': type(exc).__name__, 'apply_requested': args.apply}))
        return 1
    finally:
        if lock is not None:
            lock.release()


if __name__ == '__main__':
    raise SystemExit(main())
