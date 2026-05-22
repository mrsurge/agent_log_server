type PathLabelOptions = {
  className?: string;
  title?: string;
  strong?: boolean;
};

export function scrollPathLabelToEnd(el: HTMLElement | null | undefined): void {
  if (!(el instanceof HTMLElement)) return;
  const apply = () => {
    el.scrollLeft = el.scrollWidth;
  };
  apply();
  const win = el.ownerDocument?.defaultView;
  if (!win || typeof win.requestAnimationFrame !== 'function') return;
  win.requestAnimationFrame(() => {
    apply();
    win.requestAnimationFrame(apply);
  });
}

export function scrollPathLabelsToEnd(root: ParentNode | null | undefined): void {
  if (!root || typeof root.querySelectorAll !== 'function') return;
  root.querySelectorAll<HTMLElement>('.path-scroll-label').forEach((el) => {
    scrollPathLabelToEnd(el);
  });
}

export function applyPathScrollLabel(
  el: HTMLElement,
  label: string,
  options: PathLabelOptions = {},
): HTMLElement {
  const classes = ['path-scroll-label', options.className || '', options.strong ? 'path-scroll-strong' : '']
    .filter(Boolean)
    .join(' ');
  el.classList.add(...classes.split(/\s+/).filter(Boolean));
  el.textContent = label || 'file';
  if (options.title) {
    el.title = options.title;
  }
  scrollPathLabelToEnd(el);
  return el;
}

export function createPathScrollLabel(
  documentRef: Document,
  label: string,
  options: PathLabelOptions = {},
): HTMLSpanElement {
  const el = documentRef.createElement('span');
  applyPathScrollLabel(el, label, options);
  return el;
}

export function pathScrollLabelHtml(
  label: string,
  escapeHtml: (text: string) => string,
  options: Pick<PathLabelOptions, 'className' | 'strong'> = {},
): string {
  const classes = ['path-scroll-label', options.className || '', options.strong ? 'path-scroll-strong' : '']
    .filter(Boolean)
    .join(' ');
  return `<span class="${escapeHtml(classes)}">${escapeHtml(label || 'file')}</span>`;
}
