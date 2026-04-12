import { RPC_NAMESPACES } from './namespaces.ts';

export interface RpcMethodPlaceholder {
  name: string;
  namespace: string;
  status: 'planned';
}

export interface RpcNamespaceRegistryPlaceholder {
  namespace: string;
  methods: RpcMethodPlaceholder[];
  notifications: string[];
}

export interface RpcRegistryPlaceholder {
  status: 'placeholder';
  compatibilityNamespace: string;
  namespaces: {
    conversations: RpcNamespaceRegistryPlaceholder;
    settings: RpcNamespaceRegistryPlaceholder;
    ui: RpcNamespaceRegistryPlaceholder;
  };
}

export const RPC_REGISTRY_PLACEHOLDER: RpcRegistryPlaceholder = {
  status: 'placeholder',
  compatibilityNamespace: RPC_NAMESPACES.legacyAppserver,
  namespaces: {
    conversations: {
      namespace: RPC_NAMESPACES.conversations,
      methods: [
        { name: 'conversation.send', namespace: RPC_NAMESPACES.conversations, status: 'planned' },
        { name: 'conversation.interrupt', namespace: RPC_NAMESPACES.conversations, status: 'planned' },
        { name: 'conversation.compact', namespace: RPC_NAMESPACES.conversations, status: 'planned' },
        { name: 'conversation.replay.getChunk', namespace: RPC_NAMESPACES.conversations, status: 'planned' },
      ],
      notifications: [
        'conversation.message.delta',
        'conversation.message.final',
        'conversation.reasoning.delta',
        'conversation.reasoning.final',
        'conversation.preview.updated',
        'conversation.toast',
      ],
    },
    settings: {
      namespace: RPC_NAMESPACES.settings,
      methods: [
        { name: 'extensions.list', namespace: RPC_NAMESPACES.settings, status: 'planned' },
        { name: 'extension.settingsSchema.get', namespace: RPC_NAMESPACES.settings, status: 'planned' },
        { name: 'extension.models.list', namespace: RPC_NAMESPACES.settings, status: 'planned' },
        { name: 'extension.sessions.list', namespace: RPC_NAMESPACES.settings, status: 'planned' },
        { name: 'extension.session.bind', namespace: RPC_NAMESPACES.settings, status: 'planned' },
      ],
      notifications: [
        'extensions.updated',
        'extension.status.updated',
        'extension.runtimeOptions.updated',
      ],
    },
    ui: {
      namespace: RPC_NAMESPACES.ui,
      methods: [
        { name: 'view.set', namespace: RPC_NAMESPACES.ui, status: 'planned' },
        { name: 'hostUi.get', namespace: RPC_NAMESPACES.ui, status: 'planned' },
        { name: 'file.open', namespace: RPC_NAMESPACES.ui, status: 'planned' },
        { name: 'url.open', namespace: RPC_NAMESPACES.ui, status: 'planned' },
      ],
      notifications: [
        'view.changed',
        'hostUi.updated',
      ],
    },
  },
};

export function getRpcRegistryPlaceholder(): RpcRegistryPlaceholder {
  return RPC_REGISTRY_PLACEHOLDER;
}
