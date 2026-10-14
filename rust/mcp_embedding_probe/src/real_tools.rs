//! Isolated real-catalog integration. Not selected by any ALS provider launcher.
use pyo3::{
    prelude::*,
    types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString},
};
use rmcp::{
    ErrorData, RoleServer, ServerHandler, ServiceExt,
    model::{
        CallToolRequestParams, CallToolResponse, CallToolResult, Implementation,
        ListResourcesResult, ListToolsResult, PaginatedRequestParams, ReadResourceRequestParams,
        ReadResourceResponse, ReadResourceResult, Resource, ServerCapabilities, ServerConfig, Tool,
    },
    service::RequestContext,
};
use serde_json::{Map, Value};
use std::sync::{Arc, Mutex};
use std::{
    io,
    pin::Pin,
    task::{Context, Poll},
};
use tokio::io::{AsyncRead, ReadBuf};
use tokio::sync::{Semaphore, oneshot};

// Bound stdin lines before rmcp buffers/parses them; parsing remains rmcp-owned.
struct BoundedInput<R> {
    inner: R,
    line_bytes: usize,
}
impl<R: AsyncRead + Unpin> AsyncRead for BoundedInput<R> {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        let this = self.get_mut();
        let start = buf.filled().len();
        match Pin::new(&mut this.inner).poll_read(cx, buf) {
            Poll::Ready(Ok(())) => {
                for byte in &buf.filled()[start..] {
                    if *byte == b'\n' {
                        this.line_bytes = 0;
                    } else {
                        this.line_bytes += 1;
                    }
                    if this.line_bytes > 4 * 1024 * 1024 {
                        buf.set_filled(start);
                        return Poll::Ready(Err(io::Error::new(
                            io::ErrorKind::InvalidData,
                            "MCP stdin line exceeds 4 MiB",
                        )));
                    }
                }
                Poll::Ready(Ok(()))
            }
            result => result,
        }
    }
}

#[pyclass]
struct Completion {
    sender: Mutex<Option<oneshot::Sender<Py<PyAny>>>>,
}
#[pymethods]
impl Completion {
    fn __call__(&self, future: Py<PyAny>) {
        if let Ok(mut sender) = self.sender.lock() {
            if let Some(sender) = sender.take() {
                let _ = sender.send(future);
            }
        }
    }
}

struct Pending(Py<PyAny>);
impl Drop for Pending {
    fn drop(&mut self) {
        Python::attach(|py| {
            let _ = self.0.bind(py).call_method0("cancel");
        });
    }
}

// Conversion walks existing objects directly; no JSON dumps/loads at the ABI.
fn to_python(py: Python<'_>, value: &Value, depth: usize) -> PyResult<Py<PyAny>> {
    if depth > 64 {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "DTO nesting exceeds 64",
        ));
    }
    Ok(match value {
        Value::Null => py.None(),
        Value::Bool(v) => PyBool::new(py, *v).to_owned().into_any().unbind(),
        Value::String(v) => v.into_pyobject(py)?.into_any().unbind(),
        Value::Number(v) => {
            if let Some(v) = v.as_i64() {
                v.into_pyobject(py)?.into_any().unbind()
            } else if let Some(v) = v.as_u64() {
                v.into_pyobject(py)?.into_any().unbind()
            } else {
                v.as_f64().unwrap().into_pyobject(py)?.into_any().unbind()
            }
        }
        Value::Array(values) => {
            let list = PyList::empty(py);
            for v in values {
                list.append(to_python(py, v, depth + 1)?)?;
            }
            list.into_any().unbind()
        }
        Value::Object(values) => {
            let dict = PyDict::new(py);
            for (key, v) in values {
                dict.set_item(key, to_python(py, v, depth + 1)?)?;
            }
            dict.into_any().unbind()
        }
    })
}

fn from_python(value: &Bound<'_, PyAny>, depth: usize) -> PyResult<Value> {
    if depth > 64 {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "DTO nesting exceeds 64",
        ));
    }
    if value.is_none() {
        return Ok(Value::Null);
    }
    if value.is_instance_of::<PyBool>() {
        return Ok(Value::Bool(value.extract()?));
    }
    if value.is_instance_of::<PyString>() {
        return Ok(Value::String(value.extract()?));
    }
    if value.is_instance_of::<PyInt>() {
        if let Ok(n) = value.extract::<i64>() {
            return Ok(Value::from(n));
        }
        return Ok(Value::from(value.extract::<u64>()?));
    }
    if value.is_instance_of::<PyFloat>() {
        let n = value.extract::<f64>()?;
        return serde_json::Number::from_f64(n)
            .map(Value::Number)
            .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("nonfinite DTO number"));
    }
    if let Ok(list) = value.cast::<PyList>() {
        return list
            .iter()
            .map(|v| from_python(&v, depth + 1))
            .collect::<PyResult<Vec<_>>>()
            .map(Value::Array);
    }
    if let Ok(dict) = value.cast::<PyDict>() {
        let mut values = Map::new();
        for (key, v) in dict.iter() {
            values.insert(key.extract::<String>()?, from_python(&v, depth + 1)?);
        }
        return Ok(Value::Object(values));
    }
    Err(pyo3::exceptions::PyValueError::new_err(
        "unsupported Python DTO object",
    ))
}

#[derive(Clone)]
struct Tools {
    runtime: Arc<Py<PyAny>>,
    tools: Vec<Tool>,
    resources: Vec<Resource>,
    admission: Arc<Semaphore>,
}
impl Tools {
    async fn dispatch(
        &self,
        kind: &str,
        name: &str,
        arguments: Value,
        context: RequestContext<RoleServer>,
    ) -> Result<Value, ErrorData> {
        let _permit =
            self.admission.clone().try_acquire_owned().map_err(|_| {
                ErrorData::internal_error("MCP tool concurrency limit reached", None)
            })?;
        let (sender, receiver) = oneshot::channel();
        let future = Python::attach(|py| -> PyResult<Py<PyAny>> {
            let complete = Py::new(
                py,
                Completion {
                    sender: Mutex::new(Some(sender)),
                },
            )?;
            let arguments = to_python(py, &arguments, 0)?;
            self.runtime
                .bind(py)
                .call_method1(
                    "submit",
                    (kind, name, arguments, context.id.to_string(), complete),
                )
                .map(Bound::unbind)
        })
        .map_err(|err| ErrorData::internal_error(err.to_string(), None))?;
        let _pending = Pending(future);
        tokio::select! {
            _ = context.ct.cancelled() => Err(ErrorData::internal_error("request cancelled", None)),
            result = receiver => {
                let future = result.map_err(|_| ErrorData::internal_error("Python completion channel closed", None))?;
                Python::attach(|py| from_python(&future.bind(py).call_method0("result")?, 0))
                    .map_err(|err| ErrorData::internal_error(err.to_string(), None))
            }
        }
    }
}
impl ServerHandler for Tools {
    fn get_info(&self) -> ServerConfig {
        ServerConfig::new(
            ServerCapabilities::builder()
                .enable_tools()
                .enable_resources()
                .build(),
        )
        .with_server_info(Implementation::new(
            "agent-pty-blocks",
            "isolated-native-proof",
        ))
        .with_instructions("Agent PTY + block store tools (per-conversation).".to_owned())
    }
    async fn list_tools(
        &self,
        _: Option<PaginatedRequestParams>,
        _: RequestContext<RoleServer>,
    ) -> Result<ListToolsResult, ErrorData> {
        Ok(ListToolsResult::with_all_items(self.tools.clone()))
    }
    fn get_tool(&self, name: &str) -> Option<Tool> {
        self.tools.iter().find(|t| t.name == name).cloned()
    }
    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        context: RequestContext<RoleServer>,
    ) -> Result<CallToolResponse, ErrorData> {
        if self.get_tool(&request.name).is_none() {
            return Err(ErrorData::invalid_params("Unknown tool", None));
        }
        let value = self
            .dispatch(
                "tool",
                &request.name,
                Value::Object(request.arguments.unwrap_or_default()),
                context,
            )
            .await?;
        serde_json::from_value::<CallToolResult>(value)
            .map(Into::into)
            .map_err(|err| ErrorData::internal_error(err.to_string(), None))
    }
    async fn list_resources(
        &self,
        _: Option<PaginatedRequestParams>,
        _: RequestContext<RoleServer>,
    ) -> Result<ListResourcesResult, ErrorData> {
        Ok(ListResourcesResult::with_all_items(self.resources.clone()))
    }
    async fn read_resource(
        &self,
        request: ReadResourceRequestParams,
        context: RequestContext<RoleServer>,
    ) -> Result<ReadResourceResponse, ErrorData> {
        if !self.resources.iter().any(|r| r.uri == request.uri) {
            return Err(ErrorData::invalid_params("Unknown resource", None));
        }
        let value = self
            .dispatch("resource", &request.uri, Value::Object(Map::new()), context)
            .await?;
        serde_json::from_value::<ReadResourceResult>(value)
            .map(Into::into)
            .map_err(|err| ErrorData::internal_error(err.to_string(), None))
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Proof requires explicit package root; no cwd/PATH/remote installation discovery.
    let root = std::env::var("ALS_MCP_PYTHON_ROOT")
        .or_else(|_| std::env::var("ALS_MCP_PROBE_PYTHON_ROOT"))?;
    if std::env::args().nth(1).as_deref() == Some("--runtime-info") {
        let info = Python::attach(|py| -> PyResult<Value> {
            py.import("sys")?
                .getattr("path")?
                .call_method1("insert", (0, &root))?;
            from_python(
                &py.import("agent_log_server_rs.mcp_bootstrap")?
                    .call_method0("python_abi")?,
                0,
            )
        })?;
        println!("{}", info);
        return Ok(());
    }
    let (runtime, catalog) = Python::attach(|py| -> PyResult<_> {
        py.import("sys")?
            .getattr("path")?
            .call_method1("insert", (0, root))?;
        let runtime = py
            .import("agent_log_server_rs.mcp_embedded_runtime")?
            .getattr("Runtime")?
            .call0()?;
        let catalog = from_python(&runtime.getattr("catalog")?, 0)?;
        Ok((runtime.unbind(), catalog))
    })?;
    let owner = Tools {
        runtime: Arc::new(runtime),
        tools: serde_json::from_value(catalog["tools"].clone())?,
        resources: serde_json::from_value(catalog["resources"].clone())?,
        admission: Arc::new(Semaphore::new(32)),
    };
    let input = BoundedInput {
        inner: tokio::io::stdin(),
        line_bytes: 0,
    };
    let result = match owner.clone().serve((input, tokio::io::stdout())).await {
        Ok(service) => service
            .waiting()
            .await
            .map(|_| ())
            .map_err(|err| err.to_string()),
        Err(err) => Err(err.to_string()),
    };
    Python::attach(|py| owner.runtime.bind(py).call_method0("close").map(|_| ()))?;
    result.map_err(Into::into)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn recursive_object_conversion_preserves_dto_types() {
        Python::attach(|py| {
            let original =
                serde_json::json!({"nested": [null, true, false, "λ", -1, u64::MAX, 1.25]});
            let object = to_python(py, &original, 0).unwrap();
            assert_eq!(from_python(object.bind(py), 0).unwrap(), original);
            assert!(from_python(PyFloat::new(py, f64::NAN).as_any(), 0).is_err());
            assert!(to_python(py, &Value::Null, 65).is_err());
        });
    }

    #[tokio::test]
    async fn stdin_limit_is_per_line_and_precedes_json_parsing() {
        use tokio::io::AsyncReadExt;
        let bytes = vec![b'x'; 4 * 1024 * 1024 + 1];
        let mut input = BoundedInput {
            inner: bytes.as_slice(),
            line_bytes: 0,
        };
        let error = input.read_to_end(&mut Vec::new()).await.unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
        let mut input = BoundedInput {
            inner: b"first\nsecond\n".as_slice(),
            line_bytes: 0,
        };
        let mut result = Vec::new();
        input.read_to_end(&mut result).await.unwrap();
        assert_eq!(result, b"first\nsecond\n");
        assert_eq!(input.line_bytes, 0);
    }
}
