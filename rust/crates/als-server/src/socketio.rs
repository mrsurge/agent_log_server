use socketioxide::SocketIo;

pub fn register_socket_namespaces(io: &SocketIo) {
    io.ns(
        "/",
        |_socket: socketioxide::extract::SocketRef| async move {},
    );
}
