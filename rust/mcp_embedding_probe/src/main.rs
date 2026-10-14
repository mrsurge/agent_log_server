//! Isolated rmcp -> PyO3 -> persistent asyncio proof. Never imports ALS tools.
use pyo3::prelude::*;
use rmcp::{
    ErrorData, RoleServer, ServerHandler, ServiceExt,
    handler::server::wrapper::Parameters,
    model::{CallToolResult, ContentBlock, ServerCapabilities, ServerConfig},
    service::RequestContext,
    tool, tool_handler, tool_router,
};
use serde::Deserialize;
use std::{ffi::CString, sync::Arc, time::Duration};

#[derive(Deserialize, rmcp::schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
struct Echo {
    value: String,
}

#[derive(Deserialize, rmcp::schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
struct Wait {
    milliseconds: u32,
}

struct Pending(Py<PyAny>);
impl Drop for Pending {
    fn drop(&mut self) {
        Python::attach(|py| {
            let _ = self.0.bind(py).call_method0("cancel");
        });
    }
}

#[derive(Clone)]
struct Probe {
    runtime: Arc<Py<PyAny>>,
}

#[tool_router]
impl Probe {
    async fn finish(
        &self,
        future: Py<PyAny>,
        context: RequestContext<RoleServer>,
    ) -> Result<CallToolResult, ErrorData> {
        let pending = Pending(future);
        loop {
            let value = Python::attach(|py| -> PyResult<Option<String>> {
                let future = pending.0.bind(py);
                if future.call_method0("done")?.extract::<bool>()? {
                    Ok(Some(future.call_method0("result")?.extract()?))
                } else {
                    Ok(None)
                }
            })
            .map_err(|err| ErrorData::internal_error(err.to_string(), None))?;
            if let Some(value) = value {
                return Ok(CallToolResult::success(vec![ContentBlock::text(value)]));
            }
            tokio::select! {
                _ = context.ct.cancelled() => return Ok(CallToolResult::error(vec![ContentBlock::text("cancelled")])),
                _ = tokio::time::sleep(Duration::from_millis(2)) => {},
            }
        }
    }

    #[tool(description = "Synthetic direct Python string echo; no live ALS actions")]
    async fn probe_echo(
        &self,
        Parameters(input): Parameters<Echo>,
        context: RequestContext<RoleServer>,
    ) -> Result<CallToolResult, ErrorData> {
        let future = Python::attach(|py| {
            self.runtime
                .bind(py)
                .call_method1("submit", ("echo", input.value))
                .map(Bound::unbind)
        })
        .map_err(|err| ErrorData::internal_error(err.to_string(), None))?;
        self.finish(future, context).await
    }

    #[tool(description = "Synthetic cancellable Python asyncio wait, maximum 5000ms")]
    async fn probe_wait(
        &self,
        Parameters(input): Parameters<Wait>,
        context: RequestContext<RoleServer>,
    ) -> Result<CallToolResult, ErrorData> {
        if input.milliseconds > 5000 {
            return Err(ErrorData::invalid_params("wait exceeds proof limit", None));
        }
        let future = Python::attach(|py| {
            self.runtime
                .bind(py)
                .call_method1("submit", ("wait", input.milliseconds))
                .map(Bound::unbind)
        })
        .map_err(|err| ErrorData::internal_error(err.to_string(), None))?;
        self.finish(future, context).await
    }

    #[tool(description = "Python loop-owned synthetic active/cancelled task counters")]
    async fn probe_state(
        &self,
        context: RequestContext<RoleServer>,
    ) -> Result<CallToolResult, ErrorData> {
        let future = Python::attach(|py| {
            self.runtime
                .bind(py)
                .call_method1("submit", ("state", 0))
                .map(Bound::unbind)
        })
        .map_err(|err| ErrorData::internal_error(err.to_string(), None))?;
        self.finish(future, context).await
    }
}

#[tool_handler]
impl ServerHandler for Probe {
    fn get_info(&self) -> ServerConfig {
        ServerConfig::new(ServerCapabilities::builder().enable_tools().build())
            .with_instructions("Isolated ALS embedding proof: synthetic tools only".to_owned())
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let code = CString::new(include_str!("../runtime.py"))?;
    let runtime = Python::attach(|py| -> PyResult<Py<PyAny>> {
        let module = PyModule::from_code(
            py,
            &code,
            pyo3::ffi::c_str!("mcp_proof.py"),
            pyo3::ffi::c_str!("mcp_proof"),
        )?;
        Ok(module.getattr("Runtime")?.call0()?.unbind())
    })?;
    let probe = Probe {
        runtime: Arc::new(runtime),
    };
    let serving = probe.clone().serve(rmcp::transport::stdio()).await;
    let result = match serving {
        Ok(service) => service
            .waiting()
            .await
            .map(|_| ())
            .map_err(|err| err.to_string()),
        Err(err) => Err(err.to_string()),
    };
    // join() releases the GIL internally; Runner cancels tasks and shuts executors down.
    Python::attach(|py| probe.runtime.bind(py).call_method0("close").map(|_| ()))?;
    result.map_err(Into::into)
}
