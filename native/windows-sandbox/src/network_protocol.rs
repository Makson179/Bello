//! Fixed local protocol for the offline-network broker. No command, path,
//! permission mask, firewall action, or arbitrary SID can be supplied by a client.

use serde::{Deserialize, Serialize};

pub const BROKER_PROTOCOL_VERSION: u32 = 1;
pub const NETWORK_POLICY_VERSION: u32 = 1;
pub const SERVICE_NAME: &str = "BelloOfflineNetwork";
pub const PIPE_NAME: &str = r"\\.\pipe\Bello.OfflineNetwork.v1";
pub const MAX_BROKER_MESSAGE: usize = 16 * 1024;

#[derive(Debug, Deserialize, Serialize)]
#[serde(tag = "operation", rename_all = "camelCase", deny_unknown_fields)]
pub enum BrokerRequest {
    Status {
        #[serde(rename = "protocolVersion")]
        protocol_version: u32,
    },
    Register {
        #[serde(rename = "protocolVersion")]
        protocol_version: u32,
        #[serde(rename = "profileName")]
        profile_name: String,
        #[serde(rename = "processId")]
        process_id: u32,
        #[serde(rename = "jobHandle")]
        job_handle: u64,
    },
    Recover {
        #[serde(rename = "protocolVersion")]
        protocol_version: u32,
        #[serde(rename = "profileName")]
        profile_name: String,
    },
    Release {
        #[serde(rename = "protocolVersion")]
        protocol_version: u32,
        #[serde(rename = "profileName")]
        profile_name: String,
        #[serde(rename = "leaseId")]
        lease_id: String,
        #[serde(rename = "jobHandle")]
        job_handle: u64,
    },
}

impl BrokerRequest {
    pub fn protocol_version(&self) -> u32 {
        match self {
            Self::Status { protocol_version }
            | Self::Register {
                protocol_version, ..
            }
            | Self::Release {
                protocol_version, ..
            }
            | Self::Recover {
                protocol_version, ..
            } => *protocol_version,
        }
    }
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct BrokerReply {
    pub protocol_version: u32,
    pub policy_version: u32,
    pub service_pid: u32,
    pub result: BrokerResult,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "camelCase", deny_unknown_fields)]
pub enum BrokerResult {
    Status {
        #[serde(rename = "activeLeases")]
        active_leases: usize,
        #[serde(rename = "retainedLeases")]
        retained_leases: usize,
    },
    Registered {
        #[serde(rename = "leaseId")]
        lease_id: String,
        #[serde(rename = "filterIds")]
        filter_ids: [u64; 4],
    },
    Released,
    Recovered,
    Error {
        message: String,
    },
}

/// Stored only in administrator-owned persistent WFP filter provider data.
/// Caller identity is derived from the authenticated pipe, never its JSON.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct LeaseRecord {
    pub policy_version: u32,
    pub profile_name: String,
    pub package_sid: String,
    pub owner_sid: String,
    pub caller_pid: u32,
    pub caller_creation: u64,
    pub boot_id: String,
    pub lease_id: String,
    pub filter_keys: [String; 4],
}

#[derive(Clone, Debug)]
pub struct InstalledLease {
    pub record: LeaseRecord,
    pub filter_ids: [u64; 4],
}
