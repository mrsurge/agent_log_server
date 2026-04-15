import { RPC_NAMESPACES } from '../namespaces.ts';

export type JsonObject = Record<string, unknown>;

export const SETTINGS_RPC_NAMESPACE = RPC_NAMESPACES.settings;
export const SETTINGS_RPC_IMPLEMENTATION_STATUS = 'implemented' as const;
export const SETTINGS_RPC_METHODS = {
  configGet: 'config.get',
  configUpdate: 'config.update',
  extensionsList: 'extensions.list',
  extensionsReload: 'extensions.reload',
  extensionSettingsSchemaGet: 'extension.settingsSchema.get',
  extensionRuntimeOptionsGet: 'extension.runtimeOptions.get',
  extensionModelsList: 'extension.models.list',
  extensionSessionsList: 'extension.sessions.list',
  extensionSessionBind: 'extension.session.bind',
} as const;

export type SettingsRpcMethod =
  typeof SETTINGS_RPC_METHODS[keyof typeof SETTINGS_RPC_METHODS];

export const SETTINGS_RPC_NOTIFICATION_METHODS = [
  'extensions.updated',
  'config.updated',
] as const;

export type SettingsRpcNotificationMethod =
  typeof SETTINGS_RPC_NOTIFICATION_METHODS[number];

export const SETTINGS_RPC_ANCHOR_MODULES = [
  'settings/ui_flow.ts',
  'host/runtime.ts',
  'conversation_drawer/actions.ts',
  'settings_schema.js',
] as const;
