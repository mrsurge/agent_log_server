(function () {
  function serializerMode() {
    return String(window.AGENT_LOG_SOCKETIO_SERIALIZER || 'json').trim().toLowerCase();
  }

  function msgpackParser() {
    if (serializerMode() !== 'msgpack') return null;
    const parser = window.SocketIoMsgpackParser;
    if (parser && typeof parser.Encoder === 'function' && typeof parser.Decoder === 'function') {
      return parser;
    }
    console.warn('[socketio] msgpack requested but SocketIoMsgpackParser is unavailable');
    return null;
  }

  window.agentLogSocketIoOptions = function agentLogSocketIoOptions(options) {
    const next = Object.assign({}, options || {});
    const parser = msgpackParser();
    if (parser) next.parser = parser;
    return next;
  };
})();
