import { RPC_NAMESPACES } from './namespaces.ts';
import {
  CONVERSATIONS_RPC_METHOD_DESCRIPTORS,
  CONVERSATIONS_RPC_NOTIFICATION_METHODS,
  type ConversationsRpcMethod,
} from './conversations/contract.ts';

export type RpcSurfaceStatus = 'planned' | 'implemented';
export type RpcRegistryStatus = 'partial' | 'implemented';

export interface RpcMethodDescriptor<Name extends string = string> {
  name: Name;
  namespace: string;
  status: RpcSurfaceStatus;
}

export interface RpcNamespaceRegistry<Name extends string = string> {
  status: RpcSurfaceStatus;
  namespace: string;
  methods: readonly RpcMethodDescriptor<Name>[];
  notifications: readonly string[];
}

export interface RpcRegistry {
  status: RpcRegistryStatus;
  compatibilityNamespace: string;
  namespaces: {
    conversations: RpcNamespaceRegistry<ConversationsRpcMethod>;
    settings: RpcNamespaceRegistry;
    ui: RpcNamespaceRegistry;
  };
}

export const RPC_REGISTRY: RpcRegistry = {
  status: 'partial',
  compatibilityNamespace: RPC_NAMESPACES.legacyAppserver,
  namespaces: {
    conversations: {
      status: 'implemented',
      namespace: RPC_NAMESPACES.conversations,
      methods: CONVERSATIONS_RPC_METHOD_DESCRIPTORS,
      notifications: CONVERSATIONS_RPC_NOTIFICATION_METHODS,
    },
    settings: {
      status: 'planned',
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
      status: 'planned',
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

export function getRpcRegistry(): RpcRegistry {
  return RPC_REGISTRY;
}

export type RpcMethodPlaceholder = RpcMethodDescriptor;
export type RpcNamespaceRegistryPlaceholder = RpcNamespaceRegistry;
export type RpcRegistryPlaceholder = RpcRegistry;
export const RPC_REGISTRY_PLACEHOLDER = RPC_REGISTRY;
export const getRpcRegistryPlaceholder = getRpcRegistry;
