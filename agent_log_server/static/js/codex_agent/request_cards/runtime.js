function normalizeRequestMethod(value) {
  return typeof value === 'string' ? value.trim().toLowerCase() : '';
}

function normalizeMatchEntries(card) {
  const matches = Array.isArray(card?.matches) ? card.matches : [];
  return matches
    .filter((entry) => entry && typeof entry === 'object')
    .map((entry) => ({
      request_method: normalizeRequestMethod(entry.request_method || entry.requestMethod),
      kind: typeof entry.kind === 'string' ? entry.kind.trim() : '',
    }));
}

function requestCardProxyBase() {
  if (typeof window === 'undefined') return '';
  const path = typeof window.location?.pathname === 'string' ? window.location.pathname : '';
  const match = path.match(/^(\/api\/app\/[^/]+\/proxy)(?:\/|$)/);
  return match && match[1] ? match[1] : '';
}

function resolveRequestCardUrl(rawUrl) {
  const url = typeof rawUrl === 'string' ? rawUrl.trim() : '';
  if (!url) return '';
  if (/^(?:[a-z]+:)?\/\//i.test(url) || url.startsWith('data:') || url.startsWith('blob:')) {
    return url;
  }
  const proxyBase = requestCardProxyBase();
  if (proxyBase && url.startsWith('/')) {
    return `${proxyBase}${url}`;
  }
  return url;
}

export function bindRequestCardRuntime(ctx) {
  const { sioCall } = ctx;
  const configCache = new Map();
  const moduleCache = new Map();

  async function fetchConfig(extensionId) {
    const normalizedId = typeof extensionId === 'string' ? extensionId.trim() : '';
    if (!normalizedId) return { extension_id: '', cards: [], schemas: {} };
    if (configCache.has(normalizedId)) {
      return configCache.get(normalizedId);
    }
    const promise = (async () => {
      try {
        const data = await sioCall('get_extension_request_cards', {
          extension_id: normalizedId,
        });
        if (!data || data.ok === false) {
          return { extension_id: normalizedId, cards: [], schemas: {} };
        }
        return {
          extension_id: normalizedId,
          cards: Array.isArray(data.cards) ? data.cards : [],
          schemas: data.schemas && typeof data.schemas === 'object' ? data.schemas : {},
        };
      } catch {
        return { extension_id: normalizedId, cards: [], schemas: {} };
      }
    })();
    configCache.set(normalizedId, promise);
    return promise;
  }

  async function loadCardModule(config, card) {
    const moduleUrl = resolveRequestCardUrl(card?.module_url);
    if (!moduleUrl) return null;
    const exportName = typeof card?.export === 'string' && card.export.trim() ? card.export.trim() : 'renderRequestCard';
    const cacheKey = `${moduleUrl}#${exportName}`;
    if (moduleCache.has(cacheKey)) {
      return moduleCache.get(cacheKey);
    }
    const promise = (async () => {
      const mod = await import(moduleUrl);
      const init = mod?.initializeRequestCardModule || mod?.initializeExtensionCardModule;
      if (typeof init === 'function') {
        await init({
          extensionId: config.extension_id,
          cards: Array.isArray(config.cards) ? config.cards : [],
          schemas: config.schemas && typeof config.schemas === 'object' ? config.schemas : {},
        });
      }
      return mod;
    })();
    moduleCache.set(cacheKey, promise);
    return promise;
  }

  function findMatchingCard(config, evt) {
    const cards = Array.isArray(config?.cards) ? config.cards : [];
    const requestMethod = normalizeRequestMethod(evt?.request_method || evt?.requestMethod);
    const kind = typeof evt?.kind === 'string' ? evt.kind.trim() : '';
    return cards.find((card) => {
      const matches = normalizeMatchEntries(card);
      if (!matches.length) return false;
      return matches.some((match) => {
        if (match.request_method && match.request_method !== requestMethod) return false;
        if (match.kind && match.kind !== kind) return false;
        return true;
      });
    }) || null;
  }

  async function preload(extensionId) {
    const config = await fetchConfig(extensionId);
    const cards = Array.isArray(config.cards) ? config.cards : [];
    await Promise.all(cards.map((card) => loadCardModule(config, card).catch(() => null)));
    return config;
  }

  async function render(evt, renderCtx) {
    const extensionId = typeof renderCtx?.extensionId === 'string' && renderCtx.extensionId.trim()
      ? renderCtx.extensionId.trim()
      : '';
    if (!extensionId) return false;
    const config = await fetchConfig(extensionId);
    const card = findMatchingCard(config, evt);
    if (!card) return false;
    let mod;
    try {
      mod = await loadCardModule(config, card);
    } catch (error) {
      console.error('[request-cards] failed to load card module', {
        extensionId,
        moduleUrl: card?.module_url || null,
        error,
      });
      return false;
    }
    const exportName = typeof card.export === 'string' && card.export.trim() ? card.export.trim() : 'renderRequestCard';
    const renderFn = exportName === 'default' ? mod?.default : mod?.[exportName];
    if (typeof renderFn !== 'function') return false;
    const requestMethod = normalizeRequestMethod(evt?.request_method || evt?.requestMethod);
    const schema = requestMethod ? (config.schemas?.[requestMethod] || null) : null;
    const handled = await renderFn({
      extensionId,
      event: evt,
      card,
      config,
      schema,
      ...renderCtx,
    });
    return handled === true;
  }

  return {
    preload,
    render,
  };
}
