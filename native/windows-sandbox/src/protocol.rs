use serde::{Deserialize, Serialize};

pub const PROTOCOL_VERSION: u32 = 1;
pub const MAX_REQUEST_BYTES: usize = 1024 * 1024;

#[derive(Debug, Deserialize)]
#[serde(tag = "operation", deny_unknown_fields)]
#[cfg_attr(not(windows), allow(dead_code))]
pub enum Request {
    #[serde(rename = "run")]
    Run {
        #[serde(rename = "protocolVersion")]
        protocol_version: u32,
        command: String,
        cwd: String,
        root: String,
        mode: SandboxMode,
        #[serde(rename = "readableRoots")]
        readable_roots: Vec<String>,
        #[serde(rename = "privatePaths")]
        private_paths: Vec<String>,
        #[serde(rename = "networkAccess")]
        network_access: bool,
    },
    #[serde(rename = "recover")]
    Recover {
        #[serde(rename = "protocolVersion")]
        protocol_version: u32,
    },
}

impl Request {
    pub fn protocol_version(&self) -> u32 {
        match self {
            Self::Run {
                protocol_version, ..
            }
            | Self::Recover { protocol_version } => *protocol_version,
        }
    }
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
pub enum SandboxMode {
    #[serde(rename = "read-only")]
    ReadOnly,
    #[serde(rename = "workspace-write")]
    WorkspaceWrite,
}

#[derive(Debug, Serialize)]
#[serde(tag = "kind")]
pub enum TerminalRecord<'a> {
    #[serde(rename = "exit")]
    Exit {
        #[serde(rename = "protocolVersion")]
        protocol_version: u32,
        #[serde(rename = "exitCode")]
        exit_code: i32,
    },
    #[serde(rename = "error")]
    Error {
        #[serde(rename = "protocolVersion")]
        protocol_version: u32,
        code: &'a str,
        message: &'a str,
    },
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn run_request_is_strict_and_camel_case() {
        let request: Request = serde_json::from_str(
            r#"{"operation":"run","protocolVersion":1,"command":"echo ok","cwd":"C:\\w","root":"C:\\w","mode":"workspace-write","readableRoots":[],"privatePaths":[],"networkAccess":false}"#,
        )
        .unwrap();
        assert_eq!(request.protocol_version(), 1);
        assert!(matches!(
            request,
            Request::Run {
                network_access: false,
                ..
            }
        ));
    }

    #[test]
    fn unknown_fields_are_rejected() {
        let error = serde_json::from_str::<Request>(
            r#"{"operation":"recover","protocolVersion":1,"unexpected":true}"#,
        )
        .unwrap_err();
        assert!(error.to_string().contains("unknown field"));
    }

    #[test]
    fn terminal_record_is_unambiguous() {
        let value = serde_json::to_string(&TerminalRecord::Exit {
            protocol_version: 1,
            exit_code: 125,
        })
        .unwrap();
        assert_eq!(
            value,
            r#"{"kind":"exit","protocolVersion":1,"exitCode":125}"#
        );
    }
}
