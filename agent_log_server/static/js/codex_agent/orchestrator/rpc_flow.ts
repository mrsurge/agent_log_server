import { getRpcRegistryPlaceholder } from '../rpc/registry.ts';

const _rpcRegistryPlaceholder = getRpcRegistryPlaceholder;
void _rpcRegistryPlaceholder;

export function bindRpcFlow(ctx) {
  const {
    waitForWs,
    sioCall,
    getPending,
  } = ctx;

  let rpcId = 1;

  function nextRpcId() {
    const id = rpcId;
    rpcId += 1;
    return id;
  }

  async function sendRpc(method, params, options: Record<string, any> = {}) {
    const payload: Record<string, any> = { method };
    if (params !== undefined) payload.params = params;
    if (options.notify) {
      await sioCall('rpc', payload);
      return null;
    }
    const id = nextRpcId();
    payload.id = id;
    await waitForWs();
    await sioCall('rpc', payload);
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        getPending().delete(id);
        reject(new Error('rpc timeout'));
      }, 15000);
      getPending().set(id, { resolve, reject, timer });
    });
  }

  return {
    sendRpc,
  };
}
