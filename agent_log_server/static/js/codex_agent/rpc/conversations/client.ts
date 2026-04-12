import { RPC_NAMESPACES } from '../namespaces.ts';
import { getRpcRegistryPlaceholder } from '../registry.ts';

export interface ConversationsRpcClientPlaceholder {
  status: 'placeholder';
  namespace: string;
  methods: {
    send: 'conversation.send';
    interrupt: 'conversation.interrupt';
    compact: 'conversation.compact';
    replayGetChunk: 'conversation.replay.getChunk';
  };
  anchorModules: string[];
  notificationCount: number;
}

export function createConversationsRpcClientPlaceholder(): ConversationsRpcClientPlaceholder {
  const registry = getRpcRegistryPlaceholder();
  return {
    status: 'placeholder',
    namespace: RPC_NAMESPACES.conversations,
    methods: {
      send: 'conversation.send',
      interrupt: 'conversation.interrupt',
      compact: 'conversation.compact',
      replayGetChunk: 'conversation.replay.getChunk',
    },
    anchorModules: [
      'orchestrator/session_flow.js',
      'transcript_loader.js',
    ],
    notificationCount: registry.namespaces.conversations.notifications.length,
  };
}
