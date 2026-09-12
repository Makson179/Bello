//! A fixed, local, authenticated broker for per-AppContainer offline leases.
//! The pipe never accepts executable paths, commands, arbitrary SIDs or filters.
//! Persistent deny filters outlive a service crash. They are removed only after
//! the associated job is proved empty, or after an actual machine reboot.

use crate::identity;
use crate::network_protocol::{
    BrokerReply, BrokerRequest, BrokerResult, InstalledLease, LeaseRecord, BROKER_PROTOCOL_VERSION,
    MAX_BROKER_MESSAGE, NETWORK_POLICY_VERSION, PIPE_NAME, SERVICE_NAME,
};
use crate::offline_network;
use crate::process::Job;
use crate::winutil::{last_error, wide, Handle};
use anyhow::{anyhow, Context, Result};
use serde::de::DeserializeOwned;
use serde::Serialize;
use std::ffi::c_void;
use std::mem;
use std::os::windows::ffi::OsStringExt;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicIsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use windows_sys::Win32::Foundation::{
    CompareObjectHandles, DuplicateHandle, GetLastError, LocalFree, DUPLICATE_SAME_ACCESS,
    ERROR_IO_PENDING, ERROR_PIPE_BUSY, ERROR_PIPE_CONNECTED, FILETIME, HANDLE, WAIT_OBJECT_0,
    WAIT_TIMEOUT,
};
use windows_sys::Win32::Security::Authorization::{
    ConvertStringSecurityDescriptorToSecurityDescriptorW, GetSecurityInfo, SDDL_REVISION_1,
    SE_KERNEL_OBJECT,
};
use windows_sys::Win32::Security::{
    AccessCheck, DuplicateTokenEx, GetSidSubAuthority, GetSidSubAuthorityCount,
    GetTokenInformation, IsValidSid, RevertToSelf, SecurityIdentification, TokenAppContainerSid,
    TokenGroups, TokenImpersonation, TokenIntegrityLevel, TokenIsAppContainer, TokenUser,
    GENERIC_MAPPING, OWNER_SECURITY_INFORMATION, PRIVILEGE_SET, SECURITY_ATTRIBUTES,
    TOKEN_APPCONTAINER_INFORMATION, TOKEN_DUPLICATE, TOKEN_GROUPS, TOKEN_INFORMATION_CLASS,
    TOKEN_MANDATORY_LABEL, TOKEN_QUERY, TOKEN_USER,
};
use windows_sys::Win32::Storage::FileSystem::{
    CreateFileW, ReadFile, WriteFile, FILE_FLAG_FIRST_PIPE_INSTANCE, FILE_FLAG_OVERLAPPED,
    OPEN_EXISTING, PIPE_ACCESS_DUPLEX, SECURITY_IDENTIFICATION, SECURITY_SQOS_PRESENT,
};
use windows_sys::Win32::System::EventLog::{
    DeregisterEventSource, RegisterEventSourceW, ReportEventW, EVENTLOG_ERROR_TYPE,
};
use windows_sys::Win32::System::JobObjects::{
    IsProcessInJob, JobObjectBasicAccountingInformation, JobObjectExtendedLimitInformation,
    QueryInformationJobObject, TerminateJobObject, JOBOBJECT_BASIC_ACCOUNTING_INFORMATION,
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_BREAKAWAY_OK,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK,
};
use windows_sys::Win32::System::Pipes::{
    ConnectNamedPipe, CreateNamedPipeW, DisconnectNamedPipe, GetNamedPipeClientProcessId,
    GetNamedPipeServerProcessId, ImpersonateNamedPipeClient, WaitNamedPipeW, PIPE_READMODE_BYTE,
    PIPE_REJECT_REMOTE_CLIENTS, PIPE_TYPE_BYTE, PIPE_WAIT,
};
use windows_sys::Win32::System::Services::{
    RegisterServiceCtrlHandlerExW, SetServiceStatus, StartServiceCtrlDispatcherW,
    SERVICE_ACCEPT_SHUTDOWN, SERVICE_ACCEPT_STOP, SERVICE_CONTROL_INTERROGATE,
    SERVICE_CONTROL_SHUTDOWN, SERVICE_CONTROL_STOP, SERVICE_RUNNING, SERVICE_START_PENDING,
    SERVICE_STATUS, SERVICE_STOPPED, SERVICE_STOP_PENDING, SERVICE_TABLE_ENTRYW,
    SERVICE_WIN32_OWN_PROCESS,
};
use windows_sys::Win32::System::Threading::{
    CreateEventW, GetCurrentProcess, GetCurrentProcessId, GetCurrentThread, GetProcessTimes,
    OpenProcess, OpenProcessToken, OpenThreadToken, QueryFullProcessImageNameW,
    WaitForSingleObject, PROCESS_DUP_HANDLE, PROCESS_QUERY_LIMITED_INFORMATION,
    PROCESS_SYNCHRONIZE,
};
use windows_sys::Win32::System::IO::{CancelIoEx, GetOverlappedResultEx, OVERLAPPED};

// FILE_GENERIC_READ | FILE_GENERIC_WRITE minus FILE_APPEND_DATA, which is
// FILE_CREATE_PIPE_INSTANCE for a named pipe. In particular do NOT use GW.
const CLIENT_PIPE_ACCESS: u32 = 0x0012_019b;
const IO_TIMEOUT: Duration = Duration::from_secs(10);
const MAX_LIVE_LEASES: usize = 256;
const JOB_QUERY_TERMINATE: u32 = 0x000c;
static STOP: AtomicBool = AtomicBool::new(false);
static SERVICE_HANDLE: AtomicIsize = AtomicIsize::new(0);
struct Admission {
    accepting: bool,
    leases: usize,
}
// Serializes only lease changes and SCM admission, never sandbox execution.
static ADMISSION: Mutex<Admission> = Mutex::new(Admission {
    accepting: true,
    leases: 0,
});

fn token_data(token: HANDLE, class: TOKEN_INFORMATION_CLASS) -> Result<Vec<usize>> {
    let mut bytes = 0;
    unsafe { GetTokenInformation(token, class, std::ptr::null_mut(), 0, &mut bytes) };
    if !(4..=65536).contains(&bytes) {
        return Err(anyhow!("invalid token information size for class {class}"));
    }
    let mut data = vec![0_usize; (bytes as usize).div_ceil(mem::size_of::<usize>())];
    if unsafe { GetTokenInformation(token, class, data.as_mut_ptr().cast(), bytes, &mut bytes) }
        == 0
    {
        return Err(last_error("GetTokenInformation(network broker)"));
    }
    Ok(data)
}

fn process_token(process: HANDLE) -> Result<Handle> {
    let mut token = 0;
    if unsafe { OpenProcessToken(process, TOKEN_QUERY, &mut token) } == 0 {
        return Err(last_error("OpenProcessToken(network broker)"));
    }
    Handle::new(token, "OpenProcessToken(network broker)")
}

fn user_sid(token: HANDLE) -> Result<String> {
    let data = token_data(token, TokenUser)?;
    let user = unsafe { &*(data.as_ptr() as *const TOKEN_USER) };
    identity::sid_string(user.User.Sid)
}

fn is_appcontainer(token: HANDLE) -> Result<bool> {
    let data = token_data(token, TokenIsAppContainer)?;
    Ok(unsafe { *(data.as_ptr() as *const u32) } != 0)
}

fn validate_host_token(token: HANDLE) -> Result<String> {
    if is_appcontainer(token)? {
        return Err(anyhow!(
            "AppContainer clients cannot control offline-network leases"
        ));
    }
    let data = token_data(token, TokenIntegrityLevel)?;
    let label = unsafe { &*(data.as_ptr() as *const TOKEN_MANDATORY_LABEL) };
    if unsafe { IsValidSid(label.Label.Sid) } == 0 {
        return Err(anyhow!("invalid client integrity SID"));
    }
    let count = unsafe { *GetSidSubAuthorityCount(label.Label.Sid) };
    if count == 0 || unsafe { *GetSidSubAuthority(label.Label.Sid, u32::from(count - 1)) } < 0x2000
    {
        return Err(anyhow!(
            "offline-network client requires medium or higher integrity"
        ));
    }
    // TokenGroups works for primary and identification tokens, unlike
    // CheckTokenMembership's impersonation-token-only contract.
    let groups = token_data(token, TokenGroups)?;
    let groups = unsafe { &*(groups.as_ptr() as *const TOKEN_GROUPS) };
    if groups.GroupCount > 4096 {
        return Err(anyhow!("invalid token group count"));
    }
    let mut authenticated = false;
    for group in
        unsafe { std::slice::from_raw_parts(groups.Groups.as_ptr(), groups.GroupCount as usize) }
    {
        if group.Attributes & 0x4 != 0
            && group.Attributes & 0x10 == 0
            && identity::sid_string(group.Sid)? == "S-1-5-11"
        {
            authenticated = true;
        }
    }
    if !authenticated {
        return Err(anyhow!(
            "offline-network client is not an authenticated user"
        ));
    }
    user_sid(token)
}

fn creation_time(process: HANDLE) -> Result<u64> {
    let mut creation: FILETIME = unsafe { mem::zeroed() };
    let mut exit = creation;
    let mut kernel = creation;
    let mut user = creation;
    if unsafe { GetProcessTimes(process, &mut creation, &mut exit, &mut kernel, &mut user) } == 0 {
        return Err(last_error("GetProcessTimes(network broker)"));
    }
    Ok((u64::from(creation.dwHighDateTime) << 32) | u64::from(creation.dwLowDateTime))
}

struct Impersonation;
impl Impersonation {
    fn begin(pipe: HANDLE) -> Result<Self> {
        if unsafe { ImpersonateNamedPipeClient(pipe) } == 0 {
            return Err(last_error("ImpersonateNamedPipeClient"));
        }
        Ok(Self)
    }
}
impl Drop for Impersonation {
    fn drop(&mut self) {
        if unsafe { RevertToSelf() } == 0 {
            // Never run privileged work under an unknown impersonation state.
            // Persistent WFP filters remain installed after this fail-closed exit.
            std::process::abort();
        }
    }
}

struct Caller {
    process: Handle,
    pid: u32,
    creation: u64,
    owner: String,
}

fn authenticate(pipe: HANDLE) -> Result<Caller> {
    let mut pid = 0;
    if unsafe { GetNamedPipeClientProcessId(pipe, &mut pid) } == 0 || pid == 0 {
        return Err(last_error("GetNamedPipeClientProcessId"));
    }
    let process = Handle::new(
        unsafe {
            OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_DUP_HANDLE | PROCESS_SYNCHRONIZE,
                0,
                pid,
            )
        },
        "OpenProcess(authenticated pipe client)",
    )?;
    let impersonation = Impersonation::begin(pipe)?;
    let mut raw = 0;
    if unsafe { OpenThreadToken(GetCurrentThread(), TOKEN_QUERY, 1, &mut raw) } == 0 {
        return Err(last_error("OpenThreadToken(pipe client)"));
    }
    let token = Handle::new(raw, "OpenThreadToken(pipe client)")?;
    let owner = validate_host_token(token.raw())?;
    drop(impersonation);
    let primary = process_token(process.raw())?;
    if validate_host_token(primary.raw())? != owner {
        return Err(anyhow!(
            "pipe impersonation identity differs from its actual process"
        ));
    }
    let creation = creation_time(process.raw())?;
    if unsafe { WaitForSingleObject(process.raw(), 0) } != WAIT_TIMEOUT {
        return Err(anyhow!("offline-network client already exited"));
    }
    Ok(Caller {
        process,
        pid,
        creation,
        owner,
    })
}

#[repr(C)]
struct ObjectBasicInformation {
    attributes: u32,
    granted_access: u32,
    handle_count: u32,
    pointer_count: u32,
    reserved: [u32; 10],
}
#[link(name = "ntdll")]
extern "system" {
    fn NtQueryObject(
        handle: HANDLE,
        class: u32,
        info: *mut c_void,
        size: u32,
        returned: *mut u32,
    ) -> i32;
}

fn duplicate_job(caller: &Caller, raw: u64) -> Result<Handle> {
    let raw = isize::try_from(raw).context("invalid client job handle")?;
    if raw <= 0 {
        return Err(anyhow!("invalid client job handle"));
    }
    let mut duplicate = 0;
    if unsafe {
        DuplicateHandle(
            caller.process.raw(),
            raw,
            GetCurrentProcess(),
            &mut duplicate,
            0,
            0,
            DUPLICATE_SAME_ACCESS,
        )
    } == 0
    {
        return Err(last_error("DuplicateHandle(client job, SAME_ACCESS)"));
    }
    let job = Handle::new(duplicate, "DuplicateHandle(client job)")?;
    let mut basic: ObjectBasicInformation = unsafe { mem::zeroed() };
    let status = unsafe {
        NtQueryObject(
            job.raw(),
            0,
            (&mut basic as *mut ObjectBasicInformation).cast(),
            mem::size_of_val(&basic) as u32,
            std::ptr::null_mut(),
        )
    };
    if status < 0 || basic.granted_access & JOB_QUERY_TERMINATE != JOB_QUERY_TERMINATE {
        return Err(anyhow!(
            "client job handle must already grant QUERY and TERMINATE (NTSTATUS {status:#x})"
        ));
    }
    let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { mem::zeroed() };
    if unsafe {
        QueryInformationJobObject(
            job.raw(),
            JobObjectExtendedLimitInformation,
            (&mut limits as *mut JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
            mem::size_of_val(&limits) as u32,
            std::ptr::null_mut(),
        )
    } == 0
    {
        return Err(last_error("QueryInformationJobObject(client job limits)"));
    }
    let flags = limits.BasicLimitInformation.LimitFlags;
    if flags & JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE == 0
        || flags & (JOB_OBJECT_LIMIT_BREAKAWAY_OK | JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK) != 0
    {
        return Err(anyhow!(
            "client job must kill on close and prohibit breakaway"
        ));
    }
    Ok(job)
}

fn active_processes(job: &Handle) -> Result<u32> {
    let mut info: JOBOBJECT_BASIC_ACCOUNTING_INFORMATION = unsafe { mem::zeroed() };
    if unsafe {
        QueryInformationJobObject(
            job.raw(),
            JobObjectBasicAccountingInformation,
            (&mut info as *mut JOBOBJECT_BASIC_ACCOUNTING_INFORMATION).cast(),
            mem::size_of_val(&info) as u32,
            std::ptr::null_mut(),
        )
    } == 0
    {
        return Err(last_error("QueryInformationJobObject(lease accounting)"));
    }
    Ok(info.ActiveProcesses)
}

fn terminate_and_empty(job: &Handle) -> Result<()> {
    if active_processes(job)? == 0 {
        return Ok(());
    }
    if unsafe { TerminateJobObject(job.raw(), 125) } == 0 {
        return Err(last_error("TerminateJobObject(offline lease)"));
    }
    let deadline = Instant::now() + Duration::from_secs(5);
    while active_processes(job)? != 0 {
        if Instant::now() >= deadline {
            return Err(anyhow!(
                "offline lease job did not become empty; filters retained"
            ));
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    Ok(())
}

fn validate_child(caller: &Caller, job: &Handle, profile: &str, pid: u32) -> Result<String> {
    let expected = identity::derive_profile_sid(profile)?;
    let process = Handle::new(
        unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) },
        "OpenProcess(sandbox child)",
    )?;
    let mut in_job = 0;
    if unsafe { IsProcessInJob(process.raw(), job.raw(), &mut in_job) } == 0 || in_job != 1 {
        return Err(anyhow!(
            "sandbox child does not belong to the authenticated client's job"
        ));
    }
    let mut raw_token = 0;
    if unsafe { OpenProcessToken(process.raw(), TOKEN_QUERY | TOKEN_DUPLICATE, &mut raw_token) }
        == 0
    {
        return Err(last_error("OpenProcessToken(verified offline child)"));
    }
    let token = Handle::new(raw_token, "OpenProcessToken(verified offline child)")?;
    if !is_appcontainer(token.raw())? || user_sid(token.raw())? != caller.owner {
        return Err(anyhow!(
            "sandbox child must be an AppContainer owned by the authenticated caller"
        ));
    }
    let data = token_data(token.raw(), TokenAppContainerSid)?;
    let sid =
        unsafe { (*(data.as_ptr() as *const TOKEN_APPCONTAINER_INFORMATION)).TokenAppContainer };
    let actual = identity::sid_string(sid)?;
    if actual != identity::sid_string(expected.0)? {
        return Err(anyhow!(
            "actual sandbox package SID differs from the requested Bello profile"
        ));
    }
    validate_lpac_restriction(&token, &actual)?;
    Ok(actual)
}

fn synthetic_package_read(token: HANDLE, package: &str) -> Result<bool> {
    let mut descriptor = std::ptr::null_mut();
    // AU satisfies the ordinary-user half; the selected package satisfies the
    // AppContainer half. SYSTEM ownership cannot accidentally grant the user
    // implicit rights. There is no mandatory-label write test here.
    let sddl = format!("O:SYG:SYD:P(A;;0x1;;;AU)(A;;0x1;;;{package})");
    if unsafe {
        ConvertStringSecurityDescriptorToSecurityDescriptorW(
            wide(sddl).as_ptr(),
            SDDL_REVISION_1,
            &mut descriptor,
            std::ptr::null_mut(),
        )
    } == 0
    {
        return Err(last_error("synthetic LPAC descriptor"));
    }
    let mut privileges = [0_usize; 8192];
    let mut size = mem::size_of_val(&privileges) as u32;
    let mapping = GENERIC_MAPPING {
        GenericRead: 1,
        GenericWrite: 2,
        GenericExecute: 4,
        GenericAll: 7,
    };
    let mut granted = 0;
    let mut allowed = 0;
    let ok = unsafe {
        AccessCheck(
            descriptor,
            token,
            1,
            &mapping,
            privileges.as_mut_ptr() as *mut PRIVILEGE_SET,
            &mut size,
            &mut granted,
            &mut allowed,
        )
    };
    let code = unsafe { GetLastError() };
    unsafe { LocalFree(descriptor) };
    if ok == 0 {
        return Err(anyhow!(
            "AccessCheck(LPAC package restriction) failed with Win32 {code}"
        ));
    }
    Ok(allowed != 0 && granted & 1 == 1)
}

pub(crate) fn validate_lpac_restriction(primary: &Handle, actual_package: &str) -> Result<()> {
    let mut raw = 0;
    if unsafe {
        DuplicateTokenEx(
            primary.raw(),
            TOKEN_QUERY,
            std::ptr::null(),
            SecurityIdentification,
            TokenImpersonation,
            &mut raw,
        )
    } == 0
    {
        return Err(last_error("DuplicateTokenEx(LPAC package verification)"));
    }
    let token = Handle::new(raw, "DuplicateTokenEx(LPAC package verification)")?;
    if !synthetic_package_read(token.raw(), actual_package)? {
        return Err(anyhow!(
            "sandbox token failed the exact-package positive access control"
        ));
    }
    if synthetic_package_read(token.raw(), "S-1-15-2-1")? {
        return Err(anyhow!(
            "sandbox token accepts ALL APPLICATION PACKAGES; LPAC restriction required"
        ));
    }
    Ok(())
}

struct ActiveLease {
    lease: InstalledLease,
    caller: Caller,
    job: Handle,
}

struct BrokerState {
    boot: String,
    active: Vec<ActiveLease>,
    retained: Vec<InstalledLease>,
}

impl BrokerState {
    fn load() -> Result<Self> {
        offline_network::validate_layout()?;
        let boot = offline_network::current_boot_id()?;
        let mut retained = Vec::new();
        for lease in offline_network::leases()? {
            if lease.record.boot_id != boot {
                offline_network::remove_lease(&lease.record)?;
            } else {
                retained.push(lease);
            }
        }
        Ok(Self {
            boot,
            active: Vec::new(),
            retained,
        })
    }

    fn reap(&mut self, stopping: bool) -> Result<()> {
        let mut admission = ADMISSION
            .lock()
            .map_err(|_| anyhow!("broker admission lock poisoned"))?;
        let mut errors = Vec::new();
        for index in (0..self.active.len()).rev() {
            let entry = &self.active[index];
            let wait = unsafe { WaitForSingleObject(entry.caller.process.raw(), 0) };
            if stopping || wait == WAIT_OBJECT_0 {
                let cleanup = terminate_and_empty(&entry.job)
                    .and_then(|()| offline_network::remove_lease(&entry.lease.record));
                match cleanup {
                    Ok(()) => {
                        self.active.remove(index);
                    }
                    Err(error) => errors.push(format!("{error:#}")),
                }
            } else if wait != WAIT_TIMEOUT {
                errors.push("cannot determine lease owner liveness; filters retained".to_owned());
            }
        }
        admission.leases = self.active.len() + self.retained.len();
        if errors.is_empty() {
            Ok(())
        } else {
            Err(anyhow!(errors.join("; ")))
        }
    }

    fn request(&mut self, request: BrokerRequest, caller: Caller) -> Result<BrokerResult> {
        let mut admission = ADMISSION
            .lock()
            .map_err(|_| anyhow!("broker admission lock poisoned"))?;
        if request.protocol_version() != BROKER_PROTOCOL_VERSION {
            return Err(anyhow!("unsupported broker protocol version"));
        }
        match request {
            BrokerRequest::Status { .. } => Ok(BrokerResult::Status {
                active_leases: self.active.len(),
                retained_leases: self.retained.len(),
            }),
            BrokerRequest::Register {
                profile_name,
                process_id,
                job_handle,
                ..
            } => {
                if !admission.accepting {
                    return Err(anyhow!(
                        "network broker is stopping; command must not resume"
                    ));
                }
                identity::validate_profile_name(&profile_name)?;
                if self.active.len() + self.retained.len() >= MAX_LIVE_LEASES {
                    return Err(anyhow!(
                        "offline-network lease capacity reached; refusing unprotected execution"
                    ));
                }
                if self
                    .active
                    .iter()
                    .any(|v| v.lease.record.profile_name == profile_name)
                    || self
                        .retained
                        .iter()
                        .any(|v| v.record.profile_name == profile_name)
                {
                    return Err(anyhow!("this sandbox profile already has an offline lease"));
                }
                let job = duplicate_job(&caller, job_handle)?;
                let package_sid = validate_child(&caller, &job, &profile_name, process_id)?;
                let record = LeaseRecord {
                    policy_version: NETWORK_POLICY_VERSION,
                    profile_name,
                    package_sid,
                    owner_sid: caller.owner.clone(),
                    caller_pid: caller.pid,
                    caller_creation: caller.creation,
                    boot_id: self.boot.clone(),
                    lease_id: offline_network::new_id()?,
                    filter_keys: [
                        offline_network::new_id()?,
                        offline_network::new_id()?,
                        offline_network::new_id()?,
                        offline_network::new_id()?,
                    ],
                };
                let lease = offline_network::install_lease(record)?;
                let result = BrokerResult::Registered {
                    lease_id: lease.record.lease_id.clone(),
                    filter_ids: lease.filter_ids,
                };
                self.active.push(ActiveLease { lease, caller, job });
                admission.leases = self.active.len() + self.retained.len();
                Ok(result)
            }
            BrokerRequest::Recover { profile_name, .. } => {
                identity::validate_profile_name(&profile_name)?;
                if let Some(index) = self
                    .active
                    .iter()
                    .position(|entry| entry.lease.record.profile_name == profile_name)
                {
                    let entry = &self.active[index];
                    if entry.lease.record.owner_sid != caller.owner {
                        return Err(anyhow!("recovery profile belongs to a different account"));
                    }
                    if unsafe { WaitForSingleObject(entry.caller.process.raw(), 0) }
                        != WAIT_OBJECT_0
                    {
                        return Err(anyhow!("original sandbox helper is still alive or its death cannot be proved; recovery refused"));
                    }
                    terminate_and_empty(&entry.job)?;
                    offline_network::remove_lease(&entry.lease.record)?;
                    self.active.remove(index);
                    admission.leases = self.active.len() + self.retained.len();
                } else if let Some(entry) = self
                    .retained
                    .iter()
                    .find(|entry| entry.record.profile_name == profile_name)
                {
                    if entry.record.owner_sid != caller.owner {
                        return Err(anyhow!("recovery profile belongs to a different account"));
                    }
                    return Err(anyhow!("same-boot orphan has no provable original Job; network protection and filesystem recovery are retained until reboot"));
                }
                // This pipe is serial: no partially processed register can be
                // concurrent with an absent-lease ACK. The original helper's
                // global marker must also be absent, not merely inaccessible.
                if crate::journal::profile_marker_is_live(&profile_name)? {
                    return Err(anyhow!(
                        "original helper marker is still live; recovery refused"
                    ));
                }
                Ok(BrokerResult::Recovered)
            }
            BrokerRequest::Release {
                profile_name,
                lease_id,
                job_handle,
                ..
            } => {
                identity::validate_profile_name(&profile_name)?;
                let job = duplicate_job(&caller, job_handle)?;
                let matches = |record: &LeaseRecord| {
                    record.profile_name == profile_name
                        && record.lease_id == lease_id
                        && record.boot_id == self.boot
                        && record.owner_sid == caller.owner
                        && record.caller_pid == caller.pid
                        && record.caller_creation == caller.creation
                };
                if let Some(index) = self.active.iter().position(|v| matches(&v.lease.record)) {
                    let entry = &self.active[index];
                    if unsafe { CompareObjectHandles(job.raw(), entry.job.raw()) } == 0 {
                        return Err(anyhow!(
                            "release job differs from the originally registered job"
                        ));
                    }
                    if active_processes(&entry.job)? != 0 {
                        return Err(anyhow!(
                            "cannot release network protection while job processes remain"
                        ));
                    }
                    offline_network::remove_lease(&entry.lease.record)?;
                    self.active.remove(index);
                    admission.leases = self.active.len() + self.retained.len();
                    Ok(BrokerResult::Released)
                } else {
                    // A restarted service no longer has the original Job handle.
                    // Never guess that a caller-supplied empty job is that job.
                    Err(anyhow!("matching live offline lease not found; any same-boot orphan protection is retained until reboot"))
                }
            }
        }
    }
}

fn remaining(deadline: Instant) -> Result<u32> {
    let millis = deadline
        .saturating_duration_since(Instant::now())
        .as_millis();
    if millis == 0 {
        return Err(anyhow!("offline-network pipe deadline expired"));
    }
    Ok(millis.min(u128::from(u32::MAX - 1)) as u32)
}

struct Operation {
    overlapped: OVERLAPPED,
    _event: Handle,
}
impl Operation {
    fn new() -> Result<Self> {
        let event = Handle::new(
            unsafe { CreateEventW(std::ptr::null(), 1, 0, std::ptr::null()) },
            "CreateEventW(pipe operation)",
        )?;
        let mut overlapped: OVERLAPPED = unsafe { mem::zeroed() };
        overlapped.hEvent = event.raw();
        Ok(Self {
            overlapped,
            _event: event,
        })
    }
    fn finish(&mut self, pipe: HANDLE, deadline: Instant) -> Result<u32> {
        let mut bytes = 0;
        let wait_ms = remaining(deadline).unwrap_or(1);
        if unsafe { GetOverlappedResultEx(pipe, &self.overlapped, &mut bytes, wait_ms, 0) } != 0 {
            return Ok(bytes);
        }
        let error = last_error("GetOverlappedResultEx(network pipe)");
        unsafe { CancelIoEx(pipe, &self.overlapped) };
        // An OVERLAPPED and its buffer must remain live until the kernel has
        // completed cancellation. A broken driver must not cause use-after-free
        // or indefinite service operation: fail the process closed instead.
        if unsafe { GetOverlappedResultEx(pipe, &self.overlapped, &mut bytes, 5_000, 0) } == 0 {
            let code = unsafe { GetLastError() };
            if code == WAIT_TIMEOUT || code == 996 {
                std::process::abort();
            }
        }
        Err(error)
    }
}

fn transfer(pipe: HANDLE, bytes: &mut [u8], write: bool, deadline: Instant) -> Result<()> {
    let mut offset = 0;
    while offset < bytes.len() {
        remaining(deadline)?;
        let mut operation = Operation::new()?;
        let mut count = 0;
        let ok = unsafe {
            if write {
                WriteFile(
                    pipe,
                    bytes[offset..].as_ptr(),
                    (bytes.len() - offset) as u32,
                    &mut count,
                    &mut operation.overlapped,
                )
            } else {
                ReadFile(
                    pipe,
                    bytes[offset..].as_mut_ptr(),
                    (bytes.len() - offset) as u32,
                    &mut count,
                    &mut operation.overlapped,
                )
            }
        };
        if ok == 0 && unsafe { GetLastError() } != ERROR_IO_PENDING {
            return Err(last_error("network pipe transfer"));
        }
        // With an overlapped handle the completion record, not the optional
        // immediate byte-count output, is authoritative even on quick success.
        count = operation.finish(pipe, deadline)?;
        if count == 0 {
            return Err(anyhow!("network pipe frame ended early"));
        }
        offset += count as usize;
    }
    Ok(())
}

fn receive<T: DeserializeOwned>(pipe: HANDLE, deadline: Instant) -> Result<T> {
    let mut size = [0_u8; 4];
    transfer(pipe, &mut size, false, deadline)?;
    let size = u32::from_le_bytes(size) as usize;
    if size == 0 || size > MAX_BROKER_MESSAGE {
        return Err(anyhow!("invalid network broker frame length"));
    }
    let mut body = vec![0_u8; size];
    transfer(pipe, &mut body, false, deadline)?;
    serde_json::from_slice(&body).context("invalid network broker JSON")
}

fn send<T: Serialize>(pipe: HANDLE, value: &T, deadline: Instant) -> Result<()> {
    let mut body = serde_json::to_vec(value)?;
    if body.is_empty() || body.len() > MAX_BROKER_MESSAGE {
        return Err(anyhow!("network broker reply exceeds frame bound"));
    }
    let mut size = (body.len() as u32).to_le_bytes();
    transfer(pipe, &mut size, true, deadline)?;
    transfer(pipe, &mut body, true, deadline)
}

fn image_path(process: HANDLE) -> Result<PathBuf> {
    let mut buffer = vec![0_u16; 32768];
    let mut size = buffer.len() as u32;
    if unsafe { QueryFullProcessImageNameW(process, 0, buffer.as_mut_ptr(), &mut size) } == 0 {
        return Err(last_error("QueryFullProcessImageNameW(network service)"));
    }
    Ok(PathBuf::from(std::ffi::OsString::from_wide(
        &buffer[..size as usize],
    )))
}

fn verify_pipe_identity(
    before: &(u32, PathBuf),
    first_pipe_pid: u32,
    after: &(u32, PathBuf),
    second_pipe_pid: u32,
) -> Result<()> {
    if before.0 == 0
        || before.0 != first_pipe_pid
        || before.0 != after.0
        || before.0 != second_pipe_pid
        || !crate::winutil::path_eq(&before.1, &after.1)
    {
        return Err(anyhow!(
            "network pipe identity changed or differs from the protected running service"
        ));
    }
    Ok(())
}

fn pipe_server_pid(pipe: HANDLE) -> Result<u32> {
    let mut pid = 0;
    if unsafe { GetNamedPipeServerProcessId(pipe, &mut pid) } == 0 || pid == 0 {
        return Err(last_error("GetNamedPipeServerProcessId(network broker)"));
    }
    Ok(pid)
}

fn verify_service_self_identity() -> Result<()> {
    let (expected_pid, expected_image) = crate::network_setup::running_service_identity()?;
    let actual_pid = unsafe { GetCurrentProcessId() };
    let actual_image = std::fs::canonicalize(image_path(unsafe { GetCurrentProcess() })?)?;
    let expected_image = std::fs::canonicalize(expected_image)?;
    if actual_pid != expected_pid || !crate::winutil::path_eq(&actual_image, &expected_image) {
        return Err(anyhow!(
            "network service process differs from its protected SCM PID or image"
        ));
    }
    Ok(())
}

fn require_system_owned_pipe(pipe: HANDLE) -> Result<()> {
    // Use the pipe's existing READ_CONTROL, not foreign process/token access.
    // An unprivileged process cannot create a counterfeit SYSTEM-owned pipe.
    let mut owner = std::ptr::null_mut();
    let mut descriptor = std::ptr::null_mut();
    let code = unsafe {
        GetSecurityInfo(
            pipe,
            SE_KERNEL_OBJECT,
            OWNER_SECURITY_INFORMATION,
            &mut owner,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            &mut descriptor,
        )
    };
    let result = if code != 0 {
        Err(anyhow!(
            "GetSecurityInfo(network pipe owner) failed with Win32 {code}"
        ))
    } else if descriptor.is_null() || owner.is_null() {
        Err(anyhow!("network pipe has no valid owner descriptor"))
    } else {
        identity::sid_string(owner).and_then(|sid| {
            if sid == "S-1-5-18" {
                Ok(())
            } else {
                Err(anyhow!("network pipe is not owned by LocalSystem"))
            }
        })
    };
    if !descriptor.is_null() {
        unsafe { LocalFree(descriptor) };
    }
    result
}

fn client(request: &BrokerRequest) -> Result<BrokerReply> {
    let before = crate::network_setup::running_service_identity()?;
    // Keep the validated administrator-owned executable pinned throughout the
    // exchange. Ordinary users need only the image's existing read permission,
    // never process/token access or SeDebugPrivilege against the SYSTEM service.
    let _image = crate::winutil::open_regular_file_read(&crate::winutil::verbatim_local_absolute(
        &before.1,
    )?)?;
    let deadline = Instant::now() + IO_TIMEOUT;
    let pipe = loop {
        let raw = unsafe {
            CreateFileW(
                wide(PIPE_NAME).as_ptr(),
                CLIENT_PIPE_ACCESS,
                0,
                std::ptr::null(),
                OPEN_EXISTING,
                FILE_FLAG_OVERLAPPED | SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION,
                0,
            )
        };
        if raw != windows_sys::Win32::Foundation::INVALID_HANDLE_VALUE {
            break Handle::new(raw, "open network broker pipe")?;
        }
        let code = unsafe { GetLastError() };
        if code != ERROR_PIPE_BUSY {
            return Err(anyhow!("offline-network broker pipe unavailable (Win32 {code}); run explicit network preparation"));
        }
        if unsafe { WaitNamedPipeW(wide(PIPE_NAME).as_ptr(), remaining(deadline)?) } == 0 {
            return Err(last_error("WaitNamedPipeW(network broker)"));
        }
    };
    // Trust is anchored in SCM's protected LocalSystem/own-process/exact-image
    // configuration and the kernel-reported pipe endpoint PID, not a PID or
    // path supplied in a broker response. Recheck the fixed service after
    // connecting: a stop/restart/configuration replacement must fail closed.
    let first_pid = pipe_server_pid(pipe.raw())?;
    require_system_owned_pipe(pipe.raw())?;
    let after = crate::network_setup::running_service_identity()?;
    let actual_pid = pipe_server_pid(pipe.raw())?;
    verify_pipe_identity(&before, first_pid, &after, actual_pid)?;
    send(pipe.raw(), request, deadline)?;
    let reply: BrokerReply = receive(pipe.raw(), deadline)?;
    if reply.protocol_version != BROKER_PROTOCOL_VERSION
        || reply.policy_version != NETWORK_POLICY_VERSION
        || reply.service_pid != actual_pid
    {
        return Err(anyhow!("network broker reply identity/version mismatch"));
    }
    if let BrokerResult::Error { message } = &reply.result {
        return Err(anyhow!("offline-network broker: {message}"));
    }
    Ok(reply)
}

pub fn status() -> Result<BrokerReply> {
    client(&BrokerRequest::Status {
        protocol_version: BROKER_PROTOCOL_VERSION,
    })
}

pub fn recover(profile_name: &str) -> Result<()> {
    identity::validate_profile_name(profile_name)?;
    let reply = client(&BrokerRequest::Recover {
        protocol_version: BROKER_PROTOCOL_VERSION,
        profile_name: profile_name.to_owned(),
    })?;
    match reply.result {
        BrokerResult::Recovered => Ok(()),
        _ => Err(anyhow!("unexpected network broker recovery reply")),
    }
}

pub struct OfflineLease {
    profile: String,
    lease_id: String,
    job: Arc<Job>,
    released: bool,
}

pub fn register(profile_name: &str, child_pid: u32, job: &Arc<Job>) -> Result<OfflineLease> {
    identity::validate_profile_name(profile_name)?;
    let reply = client(&BrokerRequest::Register {
        protocol_version: BROKER_PROTOCOL_VERSION,
        profile_name: profile_name.to_owned(),
        process_id: child_pid,
        job_handle: job.raw() as u64,
    })?;
    match reply.result {
        BrokerResult::Registered {
            lease_id,
            filter_ids,
        } if !lease_id.is_empty() && filter_ids.iter().all(|id| *id != 0) => Ok(OfflineLease {
            profile: profile_name.to_owned(),
            lease_id,
            job: Arc::clone(job),
            released: false,
        }),
        _ => Err(anyhow!("unexpected network broker registration reply")),
    }
}

impl OfflineLease {
    pub fn release(&mut self) -> Result<()> {
        if self.released {
            return Ok(());
        }
        self.job.ensure_empty()?;
        let reply = client(&BrokerRequest::Release {
            protocol_version: BROKER_PROTOCOL_VERSION,
            profile_name: self.profile.clone(),
            lease_id: self.lease_id.clone(),
            job_handle: self.job.raw() as u64,
        })?;
        match reply.result {
            BrokerResult::Released => {
                self.released = true;
                Ok(())
            }
            _ => Err(anyhow!("unexpected network broker release reply")),
        }
    }
}
impl Drop for OfflineLease {
    fn drop(&mut self) {
        if !self.released && self.job.ensure_empty().is_ok() {
            // Explicit release reports errors. Error-path drop is best effort;
            // failure leaves persistent filters and the service-held Job alive.
            let _ = self.release();
        }
    }
}

fn create_pipe() -> Result<Handle> {
    let mut descriptor = std::ptr::null_mut();
    // Explicit medium mandatory label denies low-integrity writes. SYSTEM owns
    // the endpoint; AU can exchange frames but cannot create another instance.
    let sddl = "O:SYG:SYD:P(A;;GA;;;SY)(A;;0x0012019b;;;AU)S:(ML;;NW;;;ME)";
    if unsafe {
        ConvertStringSecurityDescriptorToSecurityDescriptorW(
            wide(sddl).as_ptr(),
            SDDL_REVISION_1,
            &mut descriptor,
            std::ptr::null_mut(),
        )
    } == 0
    {
        return Err(last_error("network pipe security descriptor"));
    }
    let attributes = SECURITY_ATTRIBUTES {
        nLength: mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
        lpSecurityDescriptor: descriptor,
        bInheritHandle: 0,
    };
    let raw = unsafe {
        CreateNamedPipeW(
            wide(PIPE_NAME).as_ptr(),
            PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE | FILE_FLAG_OVERLAPPED,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
            1,
            MAX_BROKER_MESSAGE as u32 + 4,
            MAX_BROKER_MESSAGE as u32 + 4,
            0,
            &attributes,
        )
    };
    let result = Handle::new(raw, "CreateNamedPipeW(exclusive local broker)");
    unsafe { LocalFree(descriptor) };
    result
}

fn serve_connection(pipe: HANDLE, state: &mut BrokerState) -> Result<()> {
    let deadline = Instant::now() + IO_TIMEOUT;
    let outcome = receive::<BrokerRequest>(pipe, deadline)
        .and_then(|request| authenticate(pipe).and_then(|caller| state.request(request, caller)));
    let result = match outcome {
        Ok(result) => result,
        Err(error) => BrokerResult::Error {
            message: format!("{error:#}"),
        },
    };
    send(
        pipe,
        &BrokerReply {
            protocol_version: BROKER_PROTOCOL_VERSION,
            policy_version: NETWORK_POLICY_VERSION,
            service_pid: unsafe { GetCurrentProcessId() },
            result,
        },
        deadline,
    )?;
    // Do not use unbounded FlushFileBuffers. A normal client closes after it has
    // read the response; wait boundedly for that EOF before DisconnectNamedPipe
    // (which otherwise could discard unread reply data).
    let mut byte = [0_u8; 1];
    let _ = transfer(pipe, &mut byte, false, deadline);
    Ok(())
}

fn report_service(state: u32, error: u32) -> Result<()> {
    let status = SERVICE_STATUS {
        dwServiceType: SERVICE_WIN32_OWN_PROCESS,
        dwCurrentState: state,
        dwControlsAccepted: if state == SERVICE_RUNNING {
            SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN
        } else {
            0
        },
        dwWin32ExitCode: error,
        dwServiceSpecificExitCode: 0,
        dwCheckPoint: if matches!(state, SERVICE_START_PENDING | SERVICE_STOP_PENDING) {
            1
        } else {
            0
        },
        dwWaitHint: if matches!(state, SERVICE_START_PENDING | SERVICE_STOP_PENDING) {
            60_000
        } else {
            0
        },
    };
    if unsafe { SetServiceStatus(SERVICE_HANDLE.load(Ordering::Acquire), &status) } == 0 {
        return Err(last_error("SetServiceStatus(network broker)"));
    }
    Ok(())
}

unsafe extern "system" fn control(control: u32, _: u32, _: *mut c_void, _: *mut c_void) -> u32 {
    match control {
        SERVICE_CONTROL_STOP | SERVICE_CONTROL_SHUTDOWN => {
            let Ok(mut admission) = ADMISSION.lock() else {
                return 1061;
            };
            if control == SERVICE_CONTROL_STOP && admission.leases != 0 {
                return 170;
            }
            admission.accepting = false;
            STOP.store(true, Ordering::Release);
            let _ = report_service(SERVICE_STOP_PENDING, 0);
            0
        }
        SERVICE_CONTROL_INTERROGATE => 0,
        _ => 120,
    }
}

fn service_loop() -> Result<()> {
    let token = process_token(unsafe { GetCurrentProcess() })?;
    if user_sid(token.raw())? != "S-1-5-18" {
        return Err(anyhow!("offline-network broker must run as LocalSystem"));
    }
    let mut state = BrokerState::load()?;
    {
        let mut admission = ADMISSION
            .lock()
            .map_err(|_| anyhow!("broker admission lock poisoned"))?;
        admission.leases = state.retained.len();
        admission.accepting = false;
    }
    let pipe = create_pipe()?;
    report_service(SERVICE_RUNNING, 0)?;
    // SCM exposes a valid running PID only after SERVICE_RUNNING. No request
    // is read or ACKed yet: verify our own image using the self pseudo-handle,
    // then atomically enable admission unless a concurrent STOP won first.
    verify_service_self_identity()?;
    {
        let mut admission = ADMISSION
            .lock()
            .map_err(|_| anyhow!("broker admission lock poisoned"))?;
        if !STOP.load(Ordering::Acquire) {
            admission.accepting = true;
        }
    }
    while !STOP.load(Ordering::Acquire) {
        state.reap(false)?;
        let mut operation = Operation::new()?;
        let connected = unsafe { ConnectNamedPipe(pipe.raw(), &mut operation.overlapped) };
        let code = unsafe { GetLastError() };
        let ready = connected != 0 || code == ERROR_PIPE_CONNECTED;
        let ready = if ready {
            true
        } else if code == ERROR_IO_PENDING {
            operation
                .finish(pipe.raw(), Instant::now() + Duration::from_millis(500))
                .is_ok()
        } else {
            return Err(anyhow!("ConnectNamedPipe failed with Win32 error {code}"));
        };
        if ready {
            let _ = serve_connection(pipe.raw(), &mut state);
        }
        unsafe { DisconnectNamedPipe(pipe.raw()) };
    }
    state.reap(true)
}

unsafe extern "system" fn service_entry(_: u32, _: *mut *mut u16) {
    let handle =
        RegisterServiceCtrlHandlerExW(wide(SERVICE_NAME).as_ptr(), Some(control), std::ptr::null());
    if handle == 0 {
        log_service_error(&format!(
            "service handler registration: {}",
            last_error("RegisterServiceCtrlHandlerExW")
        ));
        return;
    }
    SERVICE_HANDLE.store(handle, Ordering::Release);
    let _ = report_service(SERVICE_START_PENDING, 0);
    let result = std::panic::catch_unwind(service_loop);
    let code = match result {
        Ok(Ok(())) => 0,
        Ok(Err(error)) => {
            log_service_error(&format!("network service stopped after failure: {error:#}"));
            1066
        }
        Err(_) => {
            log_service_error("network service stopped after an internal panic; persistent network filters retained");
            1066
        }
    };
    let _ = report_service(SERVICE_STOPPED, code);
}

fn log_service_error(message: &str) {
    // Fixed local Application log source; no arbitrary log path, request body,
    // environment, credential, or command is recorded. Bound insertion text.
    let source = unsafe { RegisterEventSourceW(std::ptr::null(), wide(SERVICE_NAME).as_ptr()) };
    if source == 0 {
        return;
    }
    let text: Vec<u16> = message.encode_utf16().take(2048).chain(Some(0)).collect();
    let strings = [text.as_ptr()];
    unsafe {
        ReportEventW(
            source,
            EVENTLOG_ERROR_TYPE,
            0,
            1,
            std::ptr::null_mut(),
            1,
            0,
            strings.as_ptr(),
            std::ptr::null(),
        );
        DeregisterEventSource(source);
    }
}

pub fn service_main() -> Result<()> {
    STOP.store(false, Ordering::Release);
    let mut name = wide(SERVICE_NAME);
    let table = [
        SERVICE_TABLE_ENTRYW {
            lpServiceName: name.as_mut_ptr(),
            lpServiceProc: Some(service_entry),
        },
        SERVICE_TABLE_ENTRYW {
            lpServiceName: std::ptr::null_mut(),
            lpServiceProc: None,
        },
    ];
    if unsafe { StartServiceCtrlDispatcherW(table.as_ptr()) } == 0 {
        return Err(last_error("StartServiceCtrlDispatcherW"));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::process::{Command, Stdio};

    #[test]
    fn client_identity_rejects_pipe_pid_restart_or_image_change() {
        let before = (42, PathBuf::from(r"C:\Program Files\Bello\broker.exe"));
        assert!(verify_pipe_identity(&before, 42, &before, 42).is_ok());
        for (first, after, second) in [
            (41, before.clone(), 42),
            (42, (43, before.1.clone()), 42),
            (42, before.clone(), 43),
            (43, (43, before.1.clone()), 43),
            (42, (42, PathBuf::from(r"C:\Users\attacker\broker.exe")), 42),
        ] {
            assert!(verify_pipe_identity(&before, first, &after, second).is_err());
        }
        let absent = (0, before.1.clone());
        assert!(verify_pipe_identity(&absent, 0, &absent, 0).is_err());
    }

    #[test]
    fn ordinary_client_never_requests_foreign_process_or_token_access() {
        // Keep the normal-user contract explicit: its trust anchor is protected
        // SCM + kernel pipe identity. Real non-admin CI executes this path too.
        let source = include_str!("network_broker.rs");
        let client_body = source
            .split("fn client(request:")
            .nth(1)
            .unwrap()
            .split("\npub fn status(")
            .next()
            .unwrap();
        for forbidden in [
            "OpenProcess(",
            "process_token(",
            "image_path(",
            "QueryFullProcessImageNameW(",
        ] {
            assert!(
                !client_body.contains(forbidden),
                "ordinary client reintroduced {forbidden}"
            );
        }
    }

    #[test]
    fn counterfeit_user_owned_pipe_is_not_a_system_service_endpoint() {
        let token = process_token(unsafe { GetCurrentProcess() }).unwrap();
        let user = user_sid(token.raw()).unwrap();
        assert_ne!(
            user, "S-1-5-18",
            "fixture must run as an ordinary/elevated user, not SYSTEM"
        );
        let mut descriptor = std::ptr::null_mut();
        let sddl = format!("O:{user}D:P(A;;GA;;;{user})");
        assert_ne!(
            unsafe {
                ConvertStringSecurityDescriptorToSecurityDescriptorW(
                    wide(sddl).as_ptr(),
                    SDDL_REVISION_1,
                    &mut descriptor,
                    std::ptr::null_mut(),
                )
            },
            0
        );
        let attributes = SECURITY_ATTRIBUTES {
            nLength: mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: descriptor,
            bInheritHandle: 0,
        };
        let name = format!(
            r"\\.\pipe\Bello.Test.UserOwned.{}",
            identity::random_profile_name().unwrap()
        );
        let raw = unsafe {
            CreateNamedPipeW(
                wide(name).as_ptr(),
                PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE | FILE_FLAG_OVERLAPPED,
                PIPE_TYPE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
                1,
                128,
                128,
                0,
                &attributes,
            )
        };
        let pipe = Handle::new(raw, "create disposable user-owned pipe");
        unsafe { LocalFree(descriptor) };
        let pipe = pipe.unwrap();
        let error = require_system_owned_pipe(pipe.raw()).unwrap_err();
        assert!(
            error.to_string().contains("not owned by LocalSystem"),
            "{error:#}"
        );
    }

    #[test]
    fn strict_broker_protocol_rejects_paths_masks_and_unknown_versions() {
        for body in [
            r#"{"operation":"register","protocolVersion":1,"profileName":"Bello.Sandbox.0123456789abcdef0123456789abcdef","processId":1,"jobHandle":2,"path":"C:\\"}"#,
            r#"{"operation":"status","protocolVersion":1,"mask":2032127}"#,
            r#"{"operation":"release","protocolVersion":1,"profileName":"x","leaseId":"x","jobHandle":2,"packageSid":"S-1-1-0"}"#,
        ] {
            assert!(serde_json::from_str::<BrokerRequest>(body).is_err());
        }
        assert_eq!(
            CLIENT_PIPE_ACCESS & 4,
            0,
            "clients must never create pipe instances"
        );
    }

    #[test]
    fn duplicated_job_requires_existing_query_terminate_and_no_breakaway() {
        let process = Handle::new(
            unsafe {
                OpenProcess(
                    PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_DUP_HANDLE | PROCESS_SYNCHRONIZE,
                    0,
                    GetCurrentProcessId(),
                )
            },
            "test current process",
        )
        .unwrap();
        let token = process_token(process.raw()).unwrap();
        let caller = Caller {
            pid: unsafe { GetCurrentProcessId() },
            creation: creation_time(process.raw()).unwrap(),
            owner: user_sid(token.raw()).unwrap(),
            process,
        };
        let job = Job::create().unwrap();
        let duplicate = duplicate_job(&caller, job.raw() as u64).unwrap();
        assert_ne!(
            unsafe { CompareObjectHandles(job.raw(), duplicate.raw()) },
            0
        );
        assert_eq!(active_processes(&duplicate).unwrap(), 0);
        let mut limited = 0;
        assert_ne!(
            unsafe {
                DuplicateHandle(
                    GetCurrentProcess(),
                    job.raw(),
                    GetCurrentProcess(),
                    &mut limited,
                    4,
                    0,
                    0,
                )
            },
            0
        );
        let limited = Handle(limited);
        assert!(duplicate_job(&caller, limited.raw() as u64).is_err());
        assert!(duplicate_job(&caller, (-1_isize) as u64).is_err());
    }

    struct TestLease {
        child: crate::process::tests::SuspendedBrokerChild,
        lease: OfflineLease,
    }
    impl TestLease {
        fn new() -> Self {
            let child = crate::process::tests::suspended_broker_child(true);
            let lease = register(&child.profile, child.pid, &child.job).unwrap();
            Self { child, lease }
        }
        fn release_request(&self, job: &Arc<Job>) -> BrokerRequest {
            BrokerRequest::Release {
                protocol_version: BROKER_PROTOCOL_VERSION,
                profile_name: self.child.profile.clone(),
                lease_id: self.lease.lease_id.clone(),
                job_handle: job.raw() as u64,
            }
        }
    }
    impl Drop for TestLease {
        fn drop(&mut self) {
            let _ = self.child.job.terminate(125);
            let _ = self.lease.release();
        }
    }

    #[test]
    fn real_broker_rejects_active_release_and_replacement_job() {
        let mut fixture = TestLease::new();
        let forged_profile = identity::random_profile_name().unwrap();
        let forged = client(&BrokerRequest::Register {
            protocol_version: BROKER_PROTOCOL_VERSION,
            profile_name: forged_profile,
            process_id: fixture.child.pid,
            job_handle: fixture.child.job.raw() as u64,
        })
        .unwrap_err();
        assert!(
            forged
                .to_string()
                .contains("actual sandbox package SID differs"),
            "{forged:#}"
        );
        let active = client(&fixture.release_request(&fixture.child.job)).unwrap_err();
        assert!(
            active.to_string().contains("while job processes remain"),
            "{active:#}"
        );
        let unrelated = Job::create().unwrap();
        let wrong = client(&fixture.release_request(&unrelated)).unwrap_err();
        assert!(
            wrong
                .to_string()
                .contains("differs from the originally registered job"),
            "{wrong:#}"
        );
        let unrelated_child = client(&BrokerRequest::Register {
            protocol_version: BROKER_PROTOCOL_VERSION,
            profile_name: identity::random_profile_name().unwrap(),
            process_id: unsafe { GetCurrentProcessId() },
            job_handle: unrelated.raw() as u64,
        })
        .unwrap_err();
        assert!(
            unrelated_child.to_string().contains("does not belong"),
            "{unrelated_child:#}"
        );
        fixture.child.job.terminate(125).unwrap();
        fixture.lease.release().unwrap();
        fixture.lease.release().unwrap();
    }

    #[test]
    fn journal_recovery_waits_for_broker_job_empty_proof_before_acl_mutation() {
        let mut fixture = TestLease::new();
        let base = std::env::temp_dir().join(format!(
            "bello-broker-recovery-{}",
            identity::random_profile_name().unwrap()
        ));
        fs::create_dir_all(base.join("workspace")).unwrap();
        // Windows TEMP may use an 8.3 alias. Recovery intentionally validates
        // the canonical identity of every persisted path, including state.
        let base = fs::canonicalize(base).unwrap();
        let root = fs::canonicalize(base.join("workspace")).unwrap();
        let state = base.join("state");
        // Deliberately omit the helper marker: exercise the exact old race in
        // which journal recovery saw no marker but the broker still held a Job.
        let mut journal = crate::journal::Journal::create(
            &state,
            &fixture.child.profile,
            &crate::journal::mutex_name(&fixture.child.profile),
            std::slice::from_ref(&root),
        )
        .unwrap();
        journal.require_network_broker().unwrap();
        let sid = identity::derive_profile_sid(&fixture.child.profile).unwrap();
        let handle = crate::winutil::open_path(&root, true).unwrap();
        journal.before_acl_mutation(&root, &handle).unwrap();
        crate::acl::grant(&handle, sid.0, crate::protocol::SandboxMode::WorkspaceWrite).unwrap();
        let journal_path = state.join(format!("{}.json", fixture.child.profile));
        let before_journal = fs::read(&journal_path).unwrap();
        let before_acl = crate::acl::tests::paired_dacl_snapshot(&handle).unwrap();
        let error = crate::journal::recover_stale(&state).unwrap_err();
        assert!(
            format!("{error:#}").contains("original sandbox helper is still alive"),
            "{error:#}"
        );
        assert!(fixture.child.job.ensure_empty().is_err());
        assert_eq!(fs::read(&journal_path).unwrap(), before_journal);
        assert_eq!(
            crate::acl::tests::paired_dacl_snapshot(&handle).unwrap(),
            before_acl
        );
        fixture.child.job.terminate(125).unwrap();
        fixture.lease.release().unwrap();
        crate::journal::recover_stale(&state).unwrap();
        assert!(!journal_path.exists());
        crate::acl::verify_absent_object(&handle, sid.0).unwrap();
        drop(handle);
        drop(journal);
        fs::remove_dir_all(base).unwrap();
    }

    #[test]
    #[ignore = "SCM stop assertion must run serially after ordinary integration tests"]
    fn real_active_lease_refuses_service_stop() {
        use windows_sys::Win32::System::Services::{
            CloseServiceHandle, ControlService, OpenSCManagerW, OpenServiceW, SC_MANAGER_CONNECT,
            SERVICE_STOP,
        };
        let _fixture = TestLease::new();
        let scm = unsafe { OpenSCManagerW(std::ptr::null(), std::ptr::null(), SC_MANAGER_CONNECT) };
        assert_ne!(scm, 0);
        let service = unsafe { OpenServiceW(scm, wide(SERVICE_NAME).as_ptr(), SERVICE_STOP) };
        if service == 0 {
            let error = last_error("test OpenServiceW(STOP)");
            unsafe { CloseServiceHandle(scm) };
            panic!("{error:#}");
        }
        let mut service_status: SERVICE_STATUS = unsafe { mem::zeroed() };
        let stopped = unsafe { ControlService(service, SERVICE_CONTROL_STOP, &mut service_status) };
        let error = unsafe { GetLastError() };
        unsafe {
            CloseServiceHandle(service);
            CloseServiceHandle(scm);
        }
        assert_eq!(
            stopped, 0,
            "service accepted STOP while a real sandbox job was active"
        );
        assert!(matches!(error, 170 | 1061), "unexpected STOP error {error}");
        assert!(
            matches!(status().unwrap().result, BrokerResult::Status { active_leases, .. } if active_leases > 0)
        );
    }

    #[test]
    fn broker_death_owner_child() {
        let Some(marker) = std::env::var_os("BELLO_TEST_BROKER_DEATH_MARKER") else {
            return;
        };
        let fixture = TestLease::new();
        fs::write(
            marker,
            serde_json::to_vec(&(fixture.child.profile.clone(), fixture.child.pid)).unwrap(),
        )
        .unwrap();
        let mut keepalive = [0_u8; 1];
        std::io::Read::read_exact(&mut std::io::stdin(), &mut keepalive).unwrap();
    }

    #[test]
    #[ignore = "administrative WFP inspection; run serially after ordinary native tests"]
    fn real_broker_terminates_orphan_job_before_removing_lease() {
        let root = std::env::temp_dir().join(format!(
            "bello-owner-death-{}",
            identity::random_profile_name().unwrap()
        ));
        fs::create_dir(&root).unwrap();
        let marker = root.join("ready.json");
        let mut owner = Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "network_broker::tests::broker_death_owner_child",
                "--nocapture",
            ])
            .env("BELLO_TEST_BROKER_DEATH_MARKER", &marker)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let keepalive = owner.stdin.take().unwrap();
        let deadline = Instant::now() + Duration::from_secs(30);
        while !marker.exists() && Instant::now() < deadline && owner.try_wait().unwrap().is_none() {
            std::thread::sleep(Duration::from_millis(20));
        }
        if !marker.exists() {
            let _ = owner.kill();
            let output = owner.wait_with_output().unwrap();
            drop(keepalive);
            panic!("lease owner did not become ready: {output:?}");
        }
        // The creator writes a tiny complete JSON record before returning from
        // fs::write; tolerate only its visible-in-progress creation window.
        let (profile, child_pid): (String, u32) = loop {
            if let Ok(value) = serde_json::from_slice(&fs::read(&marker).unwrap()) {
                break value;
            }
            assert!(
                Instant::now() < deadline,
                "owner marker never became complete"
            );
            std::thread::sleep(Duration::from_millis(5));
        };
        let child = Handle::new(
            unsafe {
                OpenProcess(
                    PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE,
                    0,
                    child_pid,
                )
            },
            "pin orphan sandbox process",
        )
        .unwrap();
        assert!(offline_network::leases()
            .unwrap()
            .iter()
            .any(|lease| lease.record.profile_name == profile));
        owner.kill().unwrap();
        owner.wait().unwrap();
        drop(keepalive);
        let deadline = Instant::now() + Duration::from_secs(20);
        while offline_network::leases()
            .unwrap()
            .iter()
            .any(|lease| lease.record.profile_name == profile)
        {
            assert!(
                Instant::now() < deadline,
                "dead owner's offline lease was not cleaned"
            );
            std::thread::sleep(Duration::from_millis(50));
        }
        assert_eq!(
            unsafe { WaitForSingleObject(child.raw(), 0) },
            WAIT_OBJECT_0,
            "filters disappeared while orphan child remained alive"
        );
        identity::delete_profile(&profile).unwrap();
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn actual_lpac_broker_access_child() {
        if std::env::var("BELLO_TEST_LPAC_BROKER").as_deref() != Ok("1") {
            return;
        }
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            let raw = unsafe {
                CreateFileW(
                    wide(PIPE_NAME).as_ptr(),
                    CLIENT_PIPE_ACCESS,
                    0,
                    std::ptr::null(),
                    OPEN_EXISTING,
                    FILE_FLAG_OVERLAPPED | SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION,
                    0,
                )
            };
            let code = unsafe { GetLastError() };
            if raw != windows_sys::Win32::Foundation::INVALID_HANDLE_VALUE {
                drop(Handle(raw));
                panic!("actual LPAC opened the lease-control pipe with read/write access");
            }
            if code == ERROR_PIPE_BUSY && Instant::now() < deadline {
                std::thread::sleep(Duration::from_millis(10));
                continue;
            }
            assert_eq!(
                code,
                windows_sys::Win32::Foundation::ERROR_ACCESS_DENIED,
                "must prove actual access denial, not a missing service or timeout"
            );
            break;
        }
        assert!(matches!(
            fs::read(".supervisor/secret.txt").unwrap_err().kind(),
            std::io::ErrorKind::PermissionDenied
        ));
        fs::write(
            "broker-denied.txt",
            "LPAC cannot register or release any lease",
        )
        .unwrap();
    }

    #[test]
    fn actual_lpac_cannot_control_offline_broker() {
        assert!(
            matches!(status().unwrap().result, BrokerResult::Status { .. }),
            "prepare the real offline service before native integration tests"
        );
        let root = std::env::temp_dir().join(format!(
            "bello-broker-lpac-{}",
            identity::random_profile_name().unwrap()
        ));
        fs::create_dir(&root).unwrap();
        fs::create_dir(root.join(".supervisor")).unwrap();
        fs::write(root.join(".supervisor/secret.txt"), "private").unwrap();
        fs::copy(
            std::env::current_exe().unwrap(),
            root.join("broker-probe.exe"),
        )
        .unwrap();
        let mut child = Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "network_broker::tests::lpac_broker_launcher_child",
                "--nocapture",
            ])
            .env("BELLO_TEST_LPAC_BROKER_ROOT", &root)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let keepalive = child.stdin.take().unwrap();
        let deadline = Instant::now() + Duration::from_secs(60);
        while child.try_wait().unwrap().is_none() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(20));
        }
        if child.try_wait().unwrap().is_none() {
            child.kill().unwrap();
            let output = child.wait_with_output().unwrap();
            drop(keepalive);
            panic!("LPAC broker probe timed out: {output:?}");
        }
        let output = child.wait_with_output().unwrap();
        drop(keepalive);
        assert!(
            output.status.success(),
            "LPAC broker probe failed: {output:?}"
        );
        assert_eq!(
            fs::read_to_string(root.join(".supervisor/secret.txt")).unwrap(),
            "private"
        );
        assert!(root.join("broker-denied.txt").is_file());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn lpac_broker_launcher_child() {
        let Some(root) = std::env::var_os("BELLO_TEST_LPAC_BROKER_ROOT") else {
            return;
        };
        let root = fs::canonicalize(root).unwrap();
        let path = root
            .to_str()
            .unwrap()
            .strip_prefix(r"\\?\")
            .unwrap()
            .to_owned();
        let fault = std::env::var("BELLO_TEST_BROKER_FAIL_BEFORE_RESUME").as_deref() == Ok("1");
        let request = crate::protocol::Request::Run {
            protocol_version: crate::protocol::PROTOCOL_VERSION,
            command: if fault {
                "echo CHILD_WAS_RESUMED>should-not-run.txt".to_owned()
            } else {
                "set BELLO_TEST_LPAC_BROKER=1&& broker-probe.exe --exact network_broker::tests::actual_lpac_broker_access_child --nocapture".to_owned()
            },
            cwd: path.clone(),
            root: path.clone(),
            mode: crate::protocol::SandboxMode::WorkspaceWrite,
            readable_roots: Vec::new(),
            private_paths: vec![format!(r"{}\.supervisor", path)],
            network_access: false,
        };
        if fault {
            let error =
                crate::process::tests::simulate_missing_broker(|| crate::windows::execute(request))
                    .unwrap_err();
            assert!(
                format!("{error:#}").contains("command not resumed"),
                "{error:#}"
            );
            assert!(!root.join("should-not-run.txt").exists());
        } else {
            assert_eq!(crate::windows::execute(request).unwrap(), 0);
        }
    }

    #[test]
    fn missing_broker_fails_after_filesystem_setup_without_resuming_child() {
        let root = std::env::temp_dir().join(format!(
            "bello-broker-failure-{}",
            identity::random_profile_name().unwrap()
        ));
        fs::create_dir(&root).unwrap();
        fs::create_dir(root.join(".supervisor")).unwrap();
        fs::write(root.join(".supervisor/secret.txt"), "private").unwrap();
        let mut child = Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "network_broker::tests::lpac_broker_launcher_child",
                "--nocapture",
            ])
            .env("BELLO_TEST_LPAC_BROKER_ROOT", &root)
            .env("BELLO_TEST_BROKER_FAIL_BEFORE_RESUME", "1")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let keepalive = child.stdin.take().unwrap();
        let deadline = Instant::now() + Duration::from_secs(30);
        while child.try_wait().unwrap().is_none() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(20));
        }
        if child.try_wait().unwrap().is_none() {
            child.kill().unwrap();
            let output = child.wait_with_output().unwrap();
            drop(keepalive);
            panic!("before-resume failure probe timed out: {output:?}");
        }
        let output = child.wait_with_output().unwrap();
        drop(keepalive);
        assert!(
            output.status.success(),
            "before-resume failure probe failed: {output:?}"
        );
        assert!(!root.join("should-not-run.txt").exists());
        assert_eq!(
            fs::read_to_string(root.join(".supervisor/secret.txt")).unwrap(),
            "private"
        );
        fs::remove_dir_all(root).unwrap();
    }
}
