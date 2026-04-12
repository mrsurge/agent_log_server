import {
  RPC_NAMESPACES,
  type RpcNamespaceName,
} from './namespaces.ts';

export const RPC_REQUEST_EVENT = 'rpc' as const;
export const RPC_NOTIFICATION_EVENT = 'rpc.notify' as const;

export interface RpcTransportPlaceholderDescriptor {
  status: 'placeholder';
  compatibilityNamespace: string;
  requestEvent: typeof RPC_REQUEST_EVENT;
  notificationEvent: typeof RPC_NOTIFICATION_EVENT;
  publicNamespaces: {
    conversations: string;
    settings: string;
    ui: string;
  };
}

export function resolveRpcNamespace(name: RpcNamespaceName): string {
  return RPC_NAMESPACES[name];
}

export function describeRpcTransportPlaceholder(): RpcTransportPlaceholderDescriptor {
  return {
    status: 'placeholder',
    compatibilityNamespace: RPC_NAMESPACES.legacyAppserver,
    requestEvent: RPC_REQUEST_EVENT,
    notificationEvent: RPC_NOTIFICATION_EVENT,
    publicNamespaces: {
      conversations: RPC_NAMESPACES.conversations,
      settings: RPC_NAMESPACES.settings,
      ui: RPC_NAMESPACES.ui,
    },
  };
}
