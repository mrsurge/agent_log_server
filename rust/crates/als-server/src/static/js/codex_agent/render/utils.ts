import type { HighlightJsLike, UnknownRecord } from '../shared_types.ts';

declare const hljs: HighlightJsLike | undefined;

export type StructuredViewLine = {
  line_no: number;
  content: string;
};

interface RenderUtilsState {
  conversationSettings?: { cwd?: string };
  conversationMeta?: { cwd?: string };
  viewWrapEnabled?: boolean;
}

interface RenderUtilsContext {
  getState(): RenderUtilsState;
  documentRef: Document;
}

const FILE_EXT_LANG_MAP: Record<string, string> = {
  js: 'javascript', mjs: 'javascript', ts: 'typescript', tsx: 'typescript', jsx: 'javascript',
  py: 'python', rb: 'ruby', rs: 'rust', go: 'go',
  java: 'java', kt: 'kotlin', scala: 'scala',
  c: 'c', h: 'c', cpp: 'cpp', cc: 'cpp', hpp: 'cpp',
  cs: 'csharp', fs: 'fsharp',
  php: 'php', swift: 'swift', r: 'r',
  json: 'json', yaml: 'yaml', yml: 'yaml', toml: 'toml',
  xml: 'xml', html: 'html', htm: 'html', css: 'css', scss: 'scss',
  md: 'markdown', markdown: 'markdown',
  sh: 'bash', bash: 'bash', zsh: 'bash', fish: 'bash',
  sql: 'sql', graphql: 'graphql', gql: 'graphql',
  dockerfile: 'dockerfile', makefile: 'makefile',
  tf: 'hcl', hcl: 'hcl',
  lua: 'lua', vim: 'vim', el: 'lisp', clj: 'clojure',
  ex: 'elixir', exs: 'elixir', erl: 'erlang',
  hs: 'haskell', ml: 'ocaml', nim: 'nim', zig: 'zig',
};

export function bindRenderUtils(ctx: RenderUtilsContext) {
  const { getState, documentRef } = ctx;

  function escapeHtml(value: unknown) {
    return String(value ?? '').replace(/[&<>"']/g, (char) => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      '\'': '&#39;',
    }[char] || char));
  }

  function stripCitations(text: string | null | undefined) {
    if (!text) return text;
    return text.replace(/'citeturn\d+file\d+(?:L\d+(?:-L\d+)?)?'/g, '');
  }

  function detectLangFromPath(file: string | null | undefined) {
    if (!file) return null;
    const ext = file.split('.').pop()?.toLowerCase();
    if (ext && FILE_EXT_LANG_MAP[ext]) return FILE_EXT_LANG_MAP[ext];
    const basename = file.split('/').pop()?.toLowerCase();
    if (basename === 'dockerfile') return 'dockerfile';
    if (basename === 'makefile' || basename === 'gnumakefile') return 'makefile';
    if (basename?.endsWith('rc') || basename?.startsWith('.')) return 'bash';
    return null;
  }

  function resolveHljsLanguage(lang: string | null | undefined) {
    if (typeof hljs === 'undefined' || !lang) return null;
    const requested = String(lang).trim().toLowerCase();
    if (!requested) return null;
    const fallbackMap: Record<string, string[]> = {
      javascript: ['javascript', 'typescript'],
      jsx: ['javascript', 'typescript'],
      typescript: ['typescript', 'javascript'],
      tsx: ['typescript', 'javascript'],
      html: ['html', 'xml'],
      htm: ['html', 'xml'],
      xml: ['xml', 'html'],
      markdown: ['markdown'],
      md: ['markdown'],
      json: ['json'],
      css: ['css', 'scss'],
      scss: ['scss', 'css'],
      yaml: ['yaml'],
      yml: ['yaml'],
      toml: ['ini'],
      bash: ['bash'],
      sh: ['bash'],
    };
    const candidates = fallbackMap[requested] || [requested];
    for (const candidate of candidates) {
      if (candidate && hljs?.getLanguage?.(candidate)) return candidate;
    }
    return null;
  }

  function buildViewCardTitle(path: string, viewRange: unknown, fallbackTitle = '') {
    const shortPath = path ? String(path).split('/').pop() : '';
    if (
      Array.isArray(viewRange)
      && viewRange.length >= 2
      && Number.isFinite(Number(viewRange[0]))
      && Number.isFinite(Number(viewRange[1]))
    ) {
      return `${shortPath || fallbackTitle || 'view'}  Lines ${Number(viewRange[0])}–${Number(viewRange[1])}`;
    }
    if (Array.isArray(viewRange) && viewRange.length === 1 && Number.isFinite(Number(viewRange[0]))) {
      return `${shortPath || fallbackTitle || 'view'}  Line ${Number(viewRange[0])}+`;
    }
    return shortPath || fallbackTitle || 'view';
  }

  function detectLangFromCommand(command: string | null | undefined) {
    if (!command) return null;

    const shCMatch = command.match(/sh\s+-[lc]+\s+['"](.+)['"]\s*$/);
    const innerCmd = shCMatch ? shCMatch[1] : command;

    if (commandContainsGitDiff(command)) return 'diff';

    const catMatch = innerCmd.match(/\b(?:cat|head|tail|less|more|bat)\s+['"]*([^\s'"]+)/);
    if (catMatch) {
      const lang = detectLangFromPath(catMatch[1]);
      if (lang) return lang;
    }

    const sedMatch = innerCmd.match(/\bsed\s+(?:-[^\s]+\s+)*'[^']+'\s+([^\s'"]+)\s*$/);
    if (sedMatch) {
      const lang = detectLangFromPath(sedMatch[1]);
      if (lang) return lang;
    }

    const awkGrepMatch = innerCmd.match(/\b(?:awk|grep)\s+(?:-[^\s]+\s+)*(?:'[^']+'|"[^"]+")\s+([^\s'"]+)\s*$/);
    if (awkGrepMatch) {
      const lang = detectLangFromPath(awkGrepMatch[1]);
      if (lang) return lang;
    }

    const segments = innerCmd.split(/\s*(?:\|\||&&|\||;)\s*/g);
    let best = null;
    for (const segment of segments) {
      const tokens = segment.match(/(?:'[^']*'|"[^"]*"|`[^`]*`|[^\s]+)/g) || [];
      for (const token of tokens) {
        const raw = String(token || '').trim();
        if (!raw) continue;
        const unquoted = (
          (raw.startsWith('"') && raw.endsWith('"'))
          || (raw.startsWith('\'') && raw.endsWith('\''))
          || (raw.startsWith('`') && raw.endsWith('`'))
        )
          ? raw.slice(1, -1)
          : raw;
        if (unquoted.startsWith('-')) continue;
        const match = unquoted.match(/([^\s'"]+\.\w+)$/);
        if (match) {
          const lang = detectLangFromPath(match[1]);
          if (lang) best = lang;
        }
      }
    }
    if (best) return best;

    const anyFileMatch = innerCmd.match(/([^\s'"]+\.\w+)\s*$/);
    if (anyFileMatch) {
      const lang = detectLangFromPath(anyFileMatch[1]);
      if (lang) return lang;
    }

    if (innerCmd.includes('python') || innerCmd.includes('python3')) return 'python';
    if (innerCmd.includes('node ') || innerCmd.includes('npx ')) return 'javascript';
    if (innerCmd.includes('ruby ')) return 'ruby';
    if (innerCmd.includes('go run')) return 'go';
    if (innerCmd.includes('rustc') || innerCmd.includes('cargo')) return 'rust';
    return null;
  }

  function highlightCodeAlways(text: string, lang: string | null | undefined) {
    if (typeof hljs === 'undefined' || !text?.trim()) {
      return escapeHtml(text || '');
    }
    try {
      const resolvedLang = resolveHljsLanguage(lang);
      if (resolvedLang && hljs.highlight) {
        return hljs.highlight(text, { language: resolvedLang, ignoreIllegals: true }).value;
      }
      const result = hljs.highlightAuto?.(text);
      if (result && result.relevance > 5) {
        return result.value;
      }
    } catch {
      // fall through
    }
    return escapeHtml(text || '');
  }

  function normalizeStructuredViewLines(lines: unknown): StructuredViewLine[] | null {
    if (!Array.isArray(lines) || !lines.length) return null;
    const normalized: StructuredViewLine[] = [];
    for (const entry of lines) {
      if (!entry || typeof entry !== 'object') return null;
      const record = entry as UnknownRecord;
      const rawLineNo = record.line_no ?? record.lineNo;
      const lineNo = Number(rawLineNo);
      if (!Number.isFinite(lineNo)) return null;
      normalized.push({
        line_no: lineNo,
        content: record.content === null || record.content === undefined
          ? ''
          : String(record.content),
      });
    }
    return normalized;
  }

  function synthesizeStructuredViewLines(content: unknown, viewRange: unknown): StructuredViewLine[] | null {
    if (typeof content !== 'string') return null;
    if (!Array.isArray(viewRange) || !viewRange.length) return null;
    const startLine = Number(viewRange[0]);
    if (!Number.isFinite(startLine)) return null;
    if (!content) return [];
    const normalizedContent = content.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
    const rawLines = normalizedContent.split('\n');
    if (rawLines.length && rawLines[rawLines.length - 1] === '') {
      rawLines.pop();
    }
    return rawLines.map((rawLine, idx) => ({
      line_no: startLine + idx,
      content: rawLine,
    }));
  }

  function splitHighlightedHtmlIntoLines(highlightedHtml: string) {
    const html = String(highlightedHtml || '');
    const tokens = html.split(/(<[^>]+>)/g);
    const openTags: Array<{ name: string; open: string }> = [];
    const lines: string[] = [];
    let current = '';

    const closeAllTags = () => openTags.slice().reverse().map((tag) => `</${tag.name}>`).join('');
    const reopenAllTags = () => openTags.map((tag) => tag.open).join('');

    for (const token of tokens) {
      if (!token) continue;
      if (token.startsWith('<')) {
        current += token;
        if (token.startsWith('<!--') || token.startsWith('<!')) continue;
        const match = token.match(/^<\s*(\/?)\s*([a-zA-Z0-9:-]+)/);
        if (!match) continue;
        const isClosing = Boolean(match[1]);
        const tagName = String(match[2] || '').toLowerCase();
        const selfClosing = /\/\s*>$/.test(token) || ['br', 'hr', 'img', 'input', 'meta', 'link'].includes(tagName);
        if (isClosing) {
          for (let idx = openTags.length - 1; idx >= 0; idx -= 1) {
            if (openTags[idx].name === tagName) {
              openTags.splice(idx, 1);
              break;
            }
          }
        } else if (!selfClosing) {
          openTags.push({ name: tagName, open: token });
        }
        continue;
      }

      const textParts = token.split('\n');
      for (let idx = 0; idx < textParts.length; idx += 1) {
        current += textParts[idx];
        if (idx < textParts.length - 1) {
          lines.push(current + closeAllTags());
          current = reopenAllTags();
        }
      }
    }

    lines.push(current);
    return lines;
  }

  function buildHighlightedViewLineHtml(lines: StructuredViewLine[], lang: string | null) {
    if (!Array.isArray(lines) || !lines.length) return [];
    const highlighted = highlightCodeAlways(lines.map((line) => line.content).join('\n'), lang);
    const htmlLines = splitHighlightedHtmlIntoLines(highlighted);
    return htmlLines.length === lines.length ? htmlLines : [];
  }

  function getStructuredViewGutterDigits(lines: StructuredViewLine[]) {
    if (!Array.isArray(lines) || !lines.length) return 1;
    let maxDigits = 1;
    lines.forEach((line) => {
      const raw = Number(line?.line_no);
      const digits = Number.isFinite(raw) ? String(Math.abs(Math.trunc(raw))).length : 1;
      if (digits > maxDigits) maxDigits = digits;
    });
    return maxDigits;
  }

  function renderStructuredViewLineTable(lines: StructuredViewLine[], path: string) {
    const output = documentRef.createElement('div');
    output.className = 'command-output view-card-lines';
    output.classList.toggle('wrap-enabled', getState().viewWrapEnabled === true);

    const table = documentRef.createElement('table');
    table.className = 'view-card-table';
    const gutterDigits = getStructuredViewGutterDigits(lines);
    table.style.setProperty('--view-card-gutter-ch', String(gutterDigits));
    const tableBody = documentRef.createElement('tbody');

    const lang = detectLangFromPath(path);
    const highlightedLines = typeof hljs !== 'undefined' ? buildHighlightedViewLineHtml(lines, lang) : [];

    lines.forEach((line, idx) => {
      const row = documentRef.createElement('tr');
      row.className = 'view-card-line';
      row.dataset.lineNo = String(line.line_no);

      const gutter = documentRef.createElement('td');
      gutter.className = 'view-card-line-no transcript-line-no';
      gutter.dataset.lineNo = String(line.line_no);
      gutter.textContent = String(line.line_no).padStart(gutterDigits, ' ');

      const content = documentRef.createElement('td');
      content.className = 'view-card-line-content transcript-line-content';
      content.dataset.lineNo = String(line.line_no);
      const lineHtml = highlightedLines[idx];
      if (typeof lineHtml === 'string') {
        content.innerHTML = lineHtml;
      } else {
        content.textContent = line.content;
      }

      row.appendChild(gutter);
      row.appendChild(content);
      tableBody.appendChild(row);
    });

    table.appendChild(tableBody);
    output.appendChild(table);
    return output;
  }

  function renderWithHighlighting(container: HTMLElement, text: string | null | undefined) {
    if (!text) return;
    const cleanText = stripCitations(text) || '';

    const codeBlockRegex = /```(\w*)\n([\s\S]*?)```/g;
    let lastIndex = 0;
    let match: RegExpExecArray | null;
    let hasCodeBlocks = false;

    while ((match = codeBlockRegex.exec(cleanText)) !== null) {
      hasCodeBlocks = true;
      if (match.index > lastIndex) {
        const textBefore = cleanText.slice(lastIndex, match.index);
        const span = documentRef.createElement('span');
        span.textContent = textBefore;
        container.appendChild(span);
      }

      const lang = match[1] || '';
      const code = match[2];
      const pre = documentRef.createElement('pre');
      const codeEl = documentRef.createElement('code');
      if (lang) codeEl.className = `language-${lang}`;
      codeEl.textContent = code;
      pre.appendChild(codeEl);
      container.appendChild(pre);

      if (hljs?.highlightElement) {
        hljs.highlightElement(codeEl);
      }

      lastIndex = match.index + match[0].length;
    }

    if (hasCodeBlocks) {
      if (lastIndex < cleanText.length) {
        const span = documentRef.createElement('span');
        span.textContent = cleanText.slice(lastIndex);
        container.appendChild(span);
      }
    } else {
      container.textContent = cleanText;
    }
  }

  function toRelativePath(absPath: string | null | undefined) {
    if (!absPath) return '';
    const { conversationSettings = {}, conversationMeta = {} } = getState();
    const cwd = conversationSettings.cwd || conversationMeta.cwd || '';
    if (cwd && absPath.startsWith(cwd)) {
      let rel = absPath.slice(cwd.length);
      if (rel.startsWith('/')) rel = rel.slice(1);
      return rel || absPath;
    }
    const home = '/data/data/com.termux/files/home';
    if (absPath.startsWith(`${home}/`)) {
      const cwdExpanded = typeof cwd === 'string' && cwd.startsWith('~') ? `${home}${cwd.slice(1)}` : cwd;
      if (cwdExpanded && absPath.startsWith(cwdExpanded)) {
        let rel = absPath.slice(cwdExpanded.length);
        if (rel.startsWith('/')) rel = rel.slice(1);
        return rel || absPath;
      }
      return `~/${absPath.slice(home.length + 1)}`;
    }
    return absPath;
  }

  function setPill(el: HTMLElement | null, text: string, cls?: string) {
    if (!el) return;
    el.textContent = text;
    el.className = `pill ${cls || ''}`.trim();
  }

  return {
    escapeHtml,
    stripCitations,
    detectLangFromPath,
    resolveHljsLanguage,
    buildViewCardTitle,
    detectLangFromCommand,
    highlightCodeAlways,
    normalizeStructuredViewLines,
    synthesizeStructuredViewLines,
    renderStructuredViewLineTable,
    renderWithHighlighting,
    toRelativePath,
    setPill,
  };
}

export function commandContainsGitDiff(command: string | null | undefined): boolean {
  if (!command) return false;
  const shellWrapper = command.match(
    /(?:^|\s)(?:[^\s'"]*\/)?(?:ba|da|z)?sh\s+-[lc]+\s+(['"])([\s\S]*)\1\s*$/i,
  );
  const candidate = shellWrapper ? shellWrapper[2] : command;
  const tokens = candidate.match(/(?:'[^']*'|"[^"]*"|`[^`]*`|&&|\|\||[|;]|[^\s]+)/g) || [];
  let sawGit = false;
  for (const rawToken of tokens) {
    const token = rawToken.replace(/^[('"`]+|[)'"`;]+$/g, '');
    if (!token) continue;
    if (token === '&&' || token === '||' || token === '|' || token === ';') {
      sawGit = false;
      continue;
    }
    if (!sawGit) {
      const executable = token.split('/').pop()?.toLowerCase();
      sawGit = executable === 'git';
      continue;
    }
    if (token.toLowerCase() === 'diff') return true;
  }
  return false;
}
