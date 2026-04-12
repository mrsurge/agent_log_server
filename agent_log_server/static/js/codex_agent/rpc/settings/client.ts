import { RPC_NAMESPACES } from '../namespaces.ts';
import { getRpcRegistryPlaceholder } from '../registry.ts';

export interface SettingsRpcClientPlaceholder {
  status: 'placeholder';
  namespace: string;
  methods: {
    listExtensions: 'extensions.list';
    getSettingsSchema: 'extension.settingsSchema.get';
    getModels: 'extension.models.list';
    listSessions: 'extension.sessions.list';
    bindSession: 'extension.session.bind';
  };
  anchorModules: string[];
  notificationCount: number;
}

export function createSettingsRpcClientPlaceholder(): SettingsRpcClientPlaceholder {
  const registry = getRpcRegistryPlaceholder();
  return {
    status: 'placeholder',
    namespace: RPC_NAMESPACES.settings,
    methods: {
      listExtensions: 'extensions.list',
      getSettingsSchema: 'extension.settingsSchema.get',
      getModels: 'extension.models.list',
      listSessions: 'extension.sessions.list',
      bindSession: 'extension.session.bind',
    },
    anchorModules: [
      'settings/ui_flow.js',
    ],
    notificationCount: registry.namespaces.settings.notifications.length,
  };
}
