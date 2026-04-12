import { RPC_NAMESPACES } from '../namespaces.ts';
import { getRpcRegistryPlaceholder } from '../registry.ts';

export interface UiRpcClientPlaceholder {
  status: 'placeholder';
  namespace: string;
  methods: {
    setView: 'view.set';
    getHostUi: 'hostUi.get';
    openFile: 'file.open';
    openUrl: 'url.open';
  };
  anchorModules: string[];
  notificationCount: number;
}

export function createUiRpcClientPlaceholder(): UiRpcClientPlaceholder {
  const registry = getRpcRegistryPlaceholder();
  return {
    status: 'placeholder',
    namespace: RPC_NAMESPACES.ui,
    methods: {
      setView: 'view.set',
      getHostUi: 'hostUi.get',
      openFile: 'file.open',
      openUrl: 'url.open',
    },
    anchorModules: [
      'markdown.js',
      'events/socket.js',
    ],
    notificationCount: registry.namespaces.ui.notifications.length,
  };
}
