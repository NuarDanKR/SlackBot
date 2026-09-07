import { useEffect, useRef, type ReactNode } from 'react'

export function ConfirmDialog({
  open,
  title,
  detail,
  confirmLabel,
  cancelLabel = '취소',
  danger = false,
  busy = false,
  children,
  onConfirm,
  onCancel,
}: {
  open: boolean
  title: string
  detail: string
  confirmLabel: string
  cancelLabel?: string
  danger?: boolean
  busy?: boolean
  children?: ReactNode
  onConfirm: () => void
  onCancel: () => void
}) {
  const cancelRef = useRef<HTMLButtonElement>(null)
  const dialogRef = useRef<HTMLElement>(null)
  const previousFocusRef = useRef<HTMLElement | null>(null)
  const onCancelRef = useRef(onCancel)
  onCancelRef.current = onCancel

  useEffect(() => {
    if (!open) return
    previousFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
    cancelRef.current?.focus()
    return () => previousFocusRef.current?.focus()
  }, [open])

  useEffect(() => {
    if (!open) return
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !busy) onCancelRef.current()
      if (event.key !== 'Tab') return
      const controls = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), a[href]') ?? [])
      if (!controls.length) return
      const first = controls[0]
      const last = controls[controls.length - 1]
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus() }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [open, busy])

  if (!open) return null
  return <div className="dialog-backdrop" onMouseDown={(event) => {
    if (event.target === event.currentTarget && !busy) onCancel()
  }}>
    <section ref={dialogRef} className="dialog" role="dialog" aria-modal="true" aria-labelledby="confirm-dialog-title" aria-describedby="confirm-dialog-detail">
      <div className="dialog-head">
        <div className="dialog-kicker">실행 전 확인</div>
        <h2 className="dialog-title" id="confirm-dialog-title">{title}</h2>
        <p className="dialog-detail" id="confirm-dialog-detail">{detail}</p>
      </div>
      {children && <div className="dialog-body">{children}</div>}
      <div className="dialog-actions">
        <button ref={cancelRef} className="btn btn-quiet" type="button" disabled={busy} onClick={onCancel}>{cancelLabel}</button>
        <button className={`btn ${danger ? 'btn-danger' : 'btn-primary'}`} type="button" disabled={busy} onClick={onConfirm}>{busy ? '처리 중…' : confirmLabel}</button>
      </div>
    </section>
  </div>
}
