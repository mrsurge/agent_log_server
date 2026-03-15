export function bindEventRouter(ctx) {
  const {
    getState,
    setState,
    getPending,
    promptEl,
    debugEnabled,
    setLastEventType,
    setActivity,
    finalizePlanToTranscript,
    renderErrorCard,
    setStatusDot,
    renderWarningCard,
    clearReasoningRibbon,
    setReasoningRibbon,
    addMessage,
    getSubagentContainer,
    appendAssistantDelta,
    finalizeAssistant,
    appendReasoningDelta,
    finalizeReasoning,
    addDiff,
    addDeclinedDiff,
    renderApproval,
    renderCommandResult,
    renderToolBegin,
    renderToolDelta,
    renderToolEnd,
    renderToolInteraction,
    renderAgentBlockBegin,
    renderAgentBlockDelta,
    renderAgentBlockEnd,
    renderScreenDelta,
    renderAgentPtyRaw,
    renderShellBegin,
    renderShellDelta,
    renderShellEnd,
    finalizeSubagent,
    maybeAutoScroll,
    updatePlanItem,
    renderPlanCard,
    clearPlanOverlay,
    updateTokens,
    updateContextRemaining,
    renderContextCompactedCard,
    renderMetaEnvelopeInjected,
    applyHostUi,
    renderSplashTabs,
    renderConversationList,
    renderMiniConversationList,
    insertMention,
    renderPromptFromText,
  } = ctx;

  function normalizePreviewText(text, maxLen = 160) {
    if (text == null) return '';
    const normalized = String(text).replace(/\s+/g, ' ').trim();
    if (!normalized) return '';
    if (maxLen > 1 && normalized.length > maxLen) {
      return `${normalized.slice(0, maxLen - 1).trimEnd()}…`;
    }
    return normalized;
  }

  function isInternalEvent(evt) {
    if (!evt || typeof evt !== 'object') return false;
    if (evt.internal === true) return true;
    if (typeof evt.internal === 'string' && ['1', 'true', 'yes', 'on'].includes(evt.internal.trim().toLowerCase())) {
      return true;
    }
    return typeof evt.visibility === 'string' && evt.visibility.trim().toLowerCase() === 'internal';
  }

  function buildToolPreviewText(evt) {
    const toolName = typeof evt?.tool === 'string' ? evt.tool.trim() : '';
    const serverName = typeof evt?.server === 'string' ? evt.server.trim() : '';
    const args = evt?.arguments && typeof evt.arguments === 'object' ? evt.arguments : {};
    if (toolName === 'command' || toolName === 'shell') {
      const command = normalizePreviewText(args.command || evt.command || '');
      return command ? `$ ${command}` : '$ command';
    }
    if (toolName === 'web_search') {
      const query = normalizePreviewText(evt.query || args.query || '');
      return query ? `web_search: ${query}` : 'web_search';
    }
    return normalizePreviewText([serverName, toolName].filter(Boolean).join(':') || toolName || serverName || 'tool', 120);
  }

  function buildPreviewFromEvent(evt, currentPreview) {
    const evtType = typeof evt?.type === 'string' ? evt.type : '';
    switch (evtType) {
      case 'assistant_delta': {
        const sourceId = evt.id || 'assistant';
        const rawDelta = typeof evt.delta === 'string' ? evt.delta : '';
        if (!rawDelta.trim()) return null;
        const currentRaw = currentPreview?.type === 'assistant' && currentPreview?.source_id === sourceId
          ? String(currentPreview.raw_text || '')
          : '';
        const nextRaw = `${currentRaw}${rawDelta}`.slice(0, 400);
        const text = normalizePreviewText(nextRaw);
        return text ? { type: 'assistant', text, source_id: sourceId, raw_text: nextRaw } : null;
      }
      case 'assistant_finalize': {
        const rawText = typeof evt.text === 'string' ? evt.text.slice(0, 400) : '';
        const text = normalizePreviewText(rawText);
        return text ? { type: 'assistant', text, source_id: evt.id || 'assistant', raw_text: rawText } : null;
      }
      case 'message': {
        if ((evt.role || '').toLowerCase() !== 'assistant') return null;
        const rawText = typeof evt.text === 'string' ? evt.text.slice(0, 400) : '';
        const text = normalizePreviewText(rawText);
        return text ? { type: 'assistant', text, source_id: evt.id || 'assistant', raw_text: rawText } : null;
      }
      case 'tool_begin':
      case 'tool_end': {
        const text = buildToolPreviewText(evt);
        return text ? { type: 'tool', text } : null;
      }
      case 'shell_begin':
      case 'shell_end':
      case 'command_result': {
        const command = normalizePreviewText(evt.command || '', 140);
        if (command) return { type: 'tool', text: `$ ${command}` };
        const output = normalizePreviewText(evt.output || evt.stdout || evt.stderr || '', 140);
        return output ? { type: 'tool', text: output } : null;
      }
      case 'subagent_start': {
        const name = normalizePreviewText(evt.name || 'subagent', 48);
        const intent = normalizePreviewText(evt.intent || 'working', 120);
        return { type: 'subagent', text: `${name}: ${intent}` };
      }
      case 'subagent_end': {
        const summary = normalizePreviewText(evt.summary || '', 160);
        if (summary) return { type: 'subagent', text: summary };
        return { type: 'subagent', text: evt.success === false ? 'subagent failed' : 'subagent done' };
      }
      default:
        return null;
    }
  }

  function updateConversationPreview(evt) {
    const convoId = typeof evt?.conversation_id === 'string' ? evt.conversation_id.trim() : '';
    if (!convoId) return;
    const state = getState();
    const cache = state.conversationPreviewCache && typeof state.conversationPreviewCache === 'object'
      ? state.conversationPreviewCache
      : {};
    const currentPreview = cache[convoId] || null;
    const nextPreview = buildPreviewFromEvent(evt, currentPreview);
    if (!nextPreview?.text) return;
    if (
      currentPreview
      && currentPreview.type === nextPreview.type
      && currentPreview.text === nextPreview.text
      && currentPreview.source_id === nextPreview.source_id
    ) {
      return;
    }
    setState({ conversationPreviewCache: { ...cache, [convoId]: nextPreview } });
    renderConversationList(state.conversationList, state.conversationMeta?.conversation_id || null);
    renderMiniConversationList(state.conversationList, state.conversationMeta?.conversation_id || null);
  }

  function handleEvent(evt) {
    if (!evt || typeof evt !== 'object') return;
    if (isInternalEvent(evt)) return;
    const state = getState();
    updateConversationPreview(evt);

    // Filter events by conversation_id - only render events for active conversation
    const activeConvoId = state.conversationMeta?.conversation_id;
    if (evt.conversation_id && activeConvoId && evt.conversation_id !== activeConvoId) {
      return;
    }

    switch (evt.type) {
      case 'activity':
        setLastEventType('activity');
        setActivity(evt.label || 'idle', Boolean(evt.active));
        if (!evt.active && evt.label === 'idle') {
          finalizePlanToTranscript();
        }
        return;
      case 'error':
        setLastEventType('error');
        renderErrorCard(evt.message || 'Unknown error');
        setStatusDot('error');
        return;
      case 'warning':
        setLastEventType('warning');
        renderWarningCard(evt.message || '');
        setStatusDot('warning');
        return;
      case 'status':
        if (evt.status) {
          setStatusDot(evt.status);
        }
        clearReasoningRibbon();
        return;
      case 'thought':
        if (evt.text) {
          setActivity(evt.text, true);
          setReasoningRibbon(evt.text);
        }
        return;
      case 'message':
        setLastEventType('message');
        if (evt.subagent_id) {
          const sa = getSubagentContainer(evt.subagent_id, '', '');
          addMessage(evt.role || 'message', evt.text || '', sa.body);
        } else {
          addMessage(evt.role || 'message', evt.text || '');
        }
        return;
      case 'assistant_delta':
        setLastEventType('assistant');
        if (debugEnabled) console.log('[LIVE-MSG-DEBUG] assistant_delta:', evt.id, 'subagent_id:', evt.subagent_id, 'delta:', (evt.delta || '').slice(0, 50));
        if (evt.subagent_id) {
          const sa = getSubagentContainer(evt.subagent_id, '', '');
          appendAssistantDelta(evt.id, evt.delta || '', sa.body);
        } else {
          appendAssistantDelta(evt.id, evt.delta || '');
        }
        return;
      case 'assistant_finalize':
        setLastEventType('assistant');
        if (evt.subagent_id) {
          const sa = getSubagentContainer(evt.subagent_id, '', '');
          finalizeAssistant(evt.id, evt.text || '', sa.body);
        } else {
          finalizeAssistant(evt.id, evt.text || '');
        }
        setStatusDot('success');
        return;
      case 'reasoning_delta':
        setLastEventType('reasoning');
        if (evt.subagent_id) {
          const sa = getSubagentContainer(evt.subagent_id, '', '');
          appendReasoningDelta(evt.id, evt.delta || '', sa.body);
        } else {
          appendReasoningDelta(evt.id, evt.delta || '');
        }
        return;
      case 'reasoning_finalize':
        setLastEventType('reasoning');
        if (evt.subagent_id) {
          const sa = getSubagentContainer(evt.subagent_id, '', '');
          finalizeReasoning(evt.id, evt.text || '', sa.body);
        } else {
          finalizeReasoning(evt.id, evt.text || '');
        }
        return;
      case 'diff': {
        setLastEventType('diff');
        let dp = evt.path || '';
        if (!dp && evt.text) {
          const m = evt.text.match(/^diff --git a\/.+ b\/(.+)$/m);
          if (m) dp = m[1];
        }
        if (evt.subagent_id) {
          const sa = getSubagentContainer(evt.subagent_id, '', '');
          addDiff(evt.id, evt.text || '', dp, sa.body);
        } else {
          addDiff(evt.id, evt.text || '', dp);
        }
        return;
      }
      case 'diff_declined':
        setLastEventType('diff');
        addDeclinedDiff(evt.id, evt.text || '', evt.path || '');
        return;
      case 'approval':
        setLastEventType('approval');
        renderApproval(evt);
        return;
      case 'command_result':
        renderCommandResult(evt);
        return;
      case 'tool_begin':
        renderToolBegin(evt);
        return;
      case 'tool_delta':
        renderToolDelta(evt);
        return;
      case 'tool_end':
        renderToolEnd(evt);
        return;
      case 'tool_interaction':
        renderToolInteraction(evt);
        return;
      case 'agent_block_begin':
        renderAgentBlockBegin(evt);
        return;
      case 'agent_block_delta':
        renderAgentBlockDelta(evt);
        return;
      case 'agent_block_end':
        renderAgentBlockEnd(evt);
        return;
      case 'screen_delta':
        renderScreenDelta(evt);
        return;
      case 'agent_pty_raw':
        renderAgentPtyRaw(evt);
        return;
      case 'shell_begin':
        renderShellBegin(evt);
        return;
      case 'shell_delta':
        renderShellDelta(evt);
        return;
      case 'shell_end':
        renderShellEnd(evt);
        return;
      case 'subagent_start':
        setLastEventType('subagent');
        getSubagentContainer(evt.id, evt.name || 'subagent', evt.intent || 'working');
        setActivity(`subagent: ${evt.intent || evt.name || 'working'}`, true);
        maybeAutoScroll();
        return;
      case 'subagent_end':
        setLastEventType('subagent');
        finalizeSubagent(evt.id, evt.summary, evt.success);
        maybeAutoScroll();
        return;
      case 'plan_update':
        setLastEventType('plan');
        updatePlanItem(evt.step, evt.status);
        return;
      case 'plan':
        setLastEventType('plan');
        renderPlanCard(evt.steps || []);
        clearPlanOverlay();
        return;
      case 'token_count':
        setLastEventType('token');
        if (Number.isFinite(evt.context_window)) {
          setState({ contextWindow: Number(evt.context_window) });
        }
        updateTokens(evt.total);
        if (Number.isFinite(evt.context_window)) {
          updateContextRemaining(evt.total, evt.context_window);
        }
        return;
      case 'context_compacted':
        setLastEventType('system');
        renderContextCompactedCard();
        return;
      case 'meta_envelope_injected':
        setLastEventType('system');
        renderMetaEnvelopeInjected(evt);
        return;
      case 'host_ui': {
        const hostUi = {
          showClose: Boolean(evt.show_close),
          parentOrigin: (typeof evt.parent_origin === 'string' && evt.parent_origin) ? evt.parent_origin : null,
          ideMode: Boolean(evt.ide_mode),
          projectRoot: (typeof evt.project_root === 'string' && evt.project_root) ? evt.project_root : null,
        };
        setState({ hostUi });
        applyHostUi();
        const s = getState();
        if (s.activeView === 'splash' && s.hostUi?.ideMode && s.splashTab === 'project') {
          renderSplashTabs();
          renderConversationList(s.conversationList, s.conversationMeta?.conversation_id || null);
        }
        return;
      }
      case 'mention_insert': {
        const s = getState();
        if (!s.conversationMeta?.conversation_id) return;
        if (evt.conversation_id && evt.conversation_id !== s.conversationMeta.conversation_id) return;
        insertMention(evt.path || '', {
          lineNo: evt.lineNo,
          endLineNo: evt.endLineNo,
          col: evt.col,
          endCol: evt.endCol,
          content: evt.content,
        });
        return;
      }
      case 'draft_update': {
        const s = getState();
        if (!promptEl) return;
        if (!s.conversationMeta?.conversation_id) return;
        if (evt.conversation_id && evt.conversation_id !== s.conversationMeta.conversation_id) return;
        const draft = evt.draft;
        if (typeof draft !== 'string') return;
        const incomingHash = draft.split('').reduce((a, c) => ((a << 5) - a + c.charCodeAt(0)) | 0, 0).toString(16);
        if (incomingHash === s.lastDraftHash) return;
        if (s.draftDirty) {
          console.warn('Draft update ignored (local dirty)');
          return;
        }
        renderPromptFromText(draft);
        if (s.conversationMeta) s.conversationMeta.draft = draft;
        setState({ lastDraftHash: incomingHash, draftDirty: false });
        return;
      }
      case 'rpc_response': {
        const pending = getPending();
        const entry = pending.get(evt.id);
        if (entry) {
          clearTimeout(entry.timer);
          pending.delete(evt.id);
          entry.resolve(evt.result);
        }
        return;
      }
      case 'rpc_error': {
        const pending = getPending();
        const entry = pending.get(evt.id);
        if (entry) {
          clearTimeout(entry.timer);
          pending.delete(evt.id);
          if (String(evt.message || '').includes('Already initialized')) {
            entry.resolve(null);
          } else {
            entry.reject(new Error(evt.message || 'rpc error'));
          }
        }
        return;
      }
      default:
        return;
    }
  }

  return { handleEvent };
}
