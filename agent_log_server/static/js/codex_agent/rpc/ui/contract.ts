import { RPC_NAMESPACES } from '../namespaces.ts';

export type JsonObject = Record<string, unknown>;

export const UI_RPC_NAMESPACE = RPC_NAMESPACES.ui;
export const UI_RPC_IMPLEMENTATION_STATUS = 'implemented' as const;
export const UI_RPC_METHODS = {
  viewGet: 'view.get',
  viewSet: 'view.set',
  hostUiGet: 'hostUi.get',
  hostUiRecheck: 'hostUi.recheck',
  filesystemList: 'filesystem.list',
  filesystemSearch: 'filesystem.search',
  fileOpen: 'file.open',
  urlOpen: 'url.open',
} as const;

export type UiRpcMethod =
  typeof UI_RPC_METHODS[keyof typeof UI_RPC_METHODS];

export const UI_RPC_NOTIFICATION_METHODS = [
  'view.changed',
  'hostUi.updated',
] as const;

export type UiRpcNotificationMethod =
  typeof UI_RPC_NOTIFICATION_METHODS[number];

export const UI_RPC_ANCHOR_MODULES = [
  'host/runtime.ts',
  'composer/runtime.ts',
  'conversation_drawer/actions.ts',
  'markdown.js',
] as const;
