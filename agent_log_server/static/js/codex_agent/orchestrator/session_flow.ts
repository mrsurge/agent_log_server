import type { createConversationsRpcClient } from '../rpc/conversations/client.ts';
import type {
  ConversationControlResult,
  ConversationSendResult,
  JsonObject,
} from '../rpc/conversations/contract.ts';

interface SessionConversationMeta {
  conversation_id?: string | null;
}

interface SessionFlowState {
  initialized?: boolean;
  rpcTransportEnabled?: boolean;
  autoScroll?: boolean;
  conversationMeta?: SessionConversationMeta | null;
}

interface ShellBatchResult {
  exitCode: number;
  stdout: string;
  stderr: string;
}

interface ShellExecResponse extends JsonObject {
  error?: string;
  callId?: string;
  exitCode?: number;
  stdout?: string;
  stderr?: string;
}

interface ShellRowLookup {
  has(callId: string): boolean;
}

interface SessionFlowContext {
  getState: () => SessionFlowState;
  setState: (patch: Partial<SessionFlowState>) => void;
  sioCall: (event: string, payload?: JsonObject, options?: JsonObject) => Promise<unknown>;
  waitForWs: () => Promise<boolean>;
  conversationsRpcClient?: ReturnType<typeof createConversationsRpcClient> | null;
  setActivity: (label: string, active: boolean) => void;
  updateScrollButton: () => void;
  maybeAutoScroll: (force?: boolean) => void;
  renderShellBatchResult: (result: ShellBatchResult) => void;
  setStatusDot: (status: string) => void;
  shellRows: ShellRowLookup;
}

function asObject(value: unknown): JsonObject | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return null;
  }
  return value as JsonObject;
}

function normalizeLegacySendResult(
  value: unknown,
  conversationId: string,
): ConversationSendResult {
  const payload = asObject(value) ?? {};
  return {
    ...payload,
    accepted: payload.accepted === true || (payload.accepted == null && payload.ok === true),
    conversation_id: typeof payload.conversation_id === 'string' ? payload.conversation_id : conversationId,
    transport: 'legacy',
  };
}

function normalizeLegacyControlResult(value: unknown): ConversationControlResult {
  const payload = asObject(value);
  if (!payload) {
    return {
      ok: false,
      error: 'Invalid response',
      transport: 'legacy',
    };
  }
  return {
    ...payload,
    transport: 'legacy',
  };
}

function normalizeShellExecResponse(value: unknown): ShellExecResponse {
  return asObject(value) ?? {};
}

export function bindSessionFlow(ctx: SessionFlowContext) {
  const {
    getState,
    setState,
    sioCall,
    waitForWs,
    conversationsRpcClient,
    setActivity,
    updateScrollButton,
    maybeAutoScroll,
    renderShellBatchResult,
    setStatusDot,
    shellRows,
  } = ctx;

  async function ensureInitialized() {
    const state = getState();
    if (state.initialized) return;
    if (state.rpcTransportEnabled) {
      setState({ initialized: true });
      return;
    }
    await waitForWs();
    setState({ initialized: true });
  }

  async function sendUserMessage(text: string) {
    if (!text) return;
    const state = getState();
    const convoId = state.conversationMeta?.conversation_id;
    if (!convoId) {
      setActivity('save settings first', true);
      return;
    }
    setState({ autoScroll: true });
    updateScrollButton();
    maybeAutoScroll(true);
    setActivity('sending', true);
    await ensureInitialized();
    const result = conversationsRpcClient
      ? await conversationsRpcClient.sendMessage({
        conversationId: convoId,
        text,
      })
      : normalizeLegacySendResult(await sioCall('send_message', {
        conversation_id: convoId,
        text,
      }), convoId);
    if (result?.accepted === false || result?.ok === false) {
      console.error('sendUserMessage failed:', result?.error);
      setActivity(result?.error || 'send failed', true);
    }
  }

  async function sendShellCommand(command: string) {
    if (!command) return;
    command = String(command)
      .replace(/\u00A0/g, ' ')
      .replace(/[ \t]+/g, ' ')
      .trim();
    if (!command) return;
    const state = getState();
    if (!state.conversationMeta?.conversation_id) {
      setActivity('save settings first', true);
      return;
    }
    try {
      const resp = normalizeShellExecResponse(await sioCall('shell_exec', {
        conversation_id: state.conversationMeta?.conversation_id,
        command,
      }));
      const callId = typeof resp.callId === 'string' ? resp.callId : null;
      if (resp.error && (!callId || !shellRows.has(callId))) {
        renderShellBatchResult({
          exitCode: resp.exitCode || 1,
          stdout: resp.stdout || '',
          stderr: resp.stderr || resp.error,
        });
      }
    } catch (err: unknown) {
      renderShellBatchResult({
        exitCode: 1,
        stdout: '',
        stderr: String(err),
      });
      setStatusDot('error');
      setActivity('idle', false);
    }
  }

  async function interruptTurn() {
    try {
      setActivity('interrupt', true);
      const state = getState();
      const convoId = state.conversationMeta?.conversation_id || null;
      const result = conversationsRpcClient
        ? await conversationsRpcClient.interruptConversation({ conversationId: convoId })
        : normalizeLegacyControlResult(await sioCall('interrupt', convoId ? { conversation_id: convoId } : {}));
      if (result?.ok === false) {
        throw new Error(String(result?.error || 'interrupt failed'));
      }
      setActivity('interrupt sent', true);
    } catch (err: unknown) {
      console.warn('interrupt failed', err);
      setActivity('interrupt failed', true);
    }
  }

  return {
    ensureInitialized,
    sendUserMessage,
    sendShellCommand,
    interruptTurn,
  };
}
