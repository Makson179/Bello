//! Explicit administration of one fixed offline-network service. No caller
//! supplies an executable, command line, account, installation path or policy.
use crate::network_protocol::{BrokerResult, NETWORK_POLICY_VERSION, SERVICE_NAME};
use crate::winutil::{self, wide, Handle};
use anyhow::{anyhow, bail, Context, Result};
use serde::Serialize;
use std::ffi::c_void;
use std::io::{Read, Seek, Write};
use std::mem;
use std::os::windows::io::FromRawHandle;
use std::path::{Path, PathBuf};
use std::ptr;
use std::time::{Duration, Instant};
use windows_sys::Win32::Foundation::{
    GetLastError, LocalFree, ERROR_SERVICE_DOES_NOT_EXIST, GENERIC_ALL, PSID,
};
use windows_sys::Win32::Security::Authorization::{
    ConvertStringSecurityDescriptorToSecurityDescriptorW, GetSecurityInfo, SE_FILE_OBJECT,
};
use windows_sys::Win32::Security::*;
use windows_sys::Win32::Storage::FileSystem::*;
use windows_sys::Win32::System::Registry::{
    RegGetValueW, HKEY_LOCAL_MACHINE, REG_EXPAND_SZ, REG_SZ, RRF_NOEXPAND, RRF_RT_REG_EXPAND_SZ,
    RRF_RT_REG_SZ, RRF_SUBKEY_WOW6464KEY,
};
use windows_sys::Win32::System::Services::*;
use windows_sys::Win32::System::SystemInformation::GetSystemWindowsDirectoryW;

const FOLDER: &str = "BelloOfflineNetwork";
const EXECUTABLE: &str = "bello-windows-sandbox.exe";
const FILE_SD: &str = "O:BAG:BAD:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FRFX;;;BU)(A;;RC;;;OW)";
const DIRECTORY_SD: &str =
    "O:BAG:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FRFX;;;BU)(A;OICI;RC;;;OW)";
// Ordinary users can inspect service identity, never change/start/stop it.
const SERVICE_SD: &str =
    "O:BAG:BAD:P(A;;0x000f01ff;;;SY)(A;;0x000f01ff;;;BA)(A;;0x00020005;;;BU)(A;;RC;;;OW)";
const SECURITY_PARTS: u32 =
    OWNER_SECURITY_INFORMATION | GROUP_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION;

struct Local(*mut c_void);
impl Drop for Local {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe {
                LocalFree(self.0);
            }
        }
    }
}
struct Service(SC_HANDLE);
impl Drop for Service {
    fn drop(&mut self) {
        unsafe {
            CloseServiceHandle(self.0);
        }
    }
}

fn descriptor(sddl: &str) -> Result<Local> {
    let mut sd = ptr::null_mut();
    if unsafe {
        ConvertStringSecurityDescriptorToSecurityDescriptorW(
            wide(sddl).as_ptr(),
            1,
            &mut sd,
            ptr::null_mut(),
        )
    } == 0
    {
        return Err(winutil::last_error("build fixed network setup security"));
    }
    Ok(Local(sd))
}

fn same_security(actual: PSECURITY_DESCRIPTOR, expected: PSECURITY_DESCRIPTOR) -> Result<()> {
    unsafe {
        if actual.is_null()
            || expected.is_null()
            || IsValidSecurityDescriptor(actual) == 0
            || IsValidSecurityDescriptor(expected) == 0
        {
            bail!("invalid network setup security descriptor");
        }
        for getter in [GetSecurityDescriptorOwner, GetSecurityDescriptorGroup] {
            let (mut a, mut b) = (ptr::null_mut(), ptr::null_mut());
            let mut defaulted = 0;
            if getter(actual, &mut a, &mut defaulted) == 0
                || getter(expected, &mut b, &mut defaulted) == 0
                || a.is_null()
                || b.is_null()
                || IsValidSid(a) == 0
                || IsValidSid(b) == 0
                || EqualSid(a, b) == 0
            {
                bail!("network setup owner/group is not the fixed administrative identity");
            }
        }
        let (mut ac, mut ec, mut revision) = (0, 0, 0);
        if GetSecurityDescriptorControl(actual, &mut ac, &mut revision) == 0
            || GetSecurityDescriptorControl(expected, &mut ec, &mut revision) == 0
            || ac & SE_DACL_PROTECTED != ec & SE_DACL_PROTECTED
        {
            bail!("network setup permissions must be protected from inheritance");
        }
        let (mut aa, mut ea) = (ptr::null_mut(), ptr::null_mut());
        let (mut present, mut defaulted) = (0, 0);
        if GetSecurityDescriptorDacl(actual, &mut present, &mut aa, &mut defaulted) == 0
            || present == 0
            || aa.is_null()
            || IsValidAcl(aa) == 0
            || GetSecurityDescriptorDacl(expected, &mut present, &mut ea, &mut defaulted) == 0
            || present == 0
            || ea.is_null()
            || IsValidAcl(ea) == 0
            || (*aa).AceCount != (*ea).AceCount
        {
            bail!("network setup permissions differ from the fixed policy");
        }
        for i in 0..(*aa).AceCount as u32 {
            let (mut a, mut b) = (ptr::null_mut(), ptr::null_mut());
            if GetAce(aa, i, &mut a) == 0
                || GetAce(ea, i, &mut b) == 0
                || a.is_null()
                || b.is_null()
            {
                return Err(winutil::last_error("GetAce(network setup)"));
            }
            let size = (*(a as *const ACE_HEADER)).AceSize as usize;
            if size != (*(b as *const ACE_HEADER)).AceSize as usize
                || std::slice::from_raw_parts(a as *const u8, size)
                    != std::slice::from_raw_parts(b as *const u8, size)
            {
                bail!("network setup permissions contain an unexpected grant");
            }
        }
    }
    Ok(())
}

fn validate_object(path: &Path, directory: bool) -> Result<Handle> {
    let normalized = winutil::verbatim_local_absolute(path)?;
    let handle = winutil::open_path(&normalized, false)?;
    winutil::validate_final_path(&handle, &normalized)?;
    winutil::validate_plain_file_object(&handle, &normalized)?;
    if (winutil::file_info(&handle)?.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY != 0) != directory
    {
        bail!("unexpected network setup object type: {}", path.display());
    }
    let mut sd = ptr::null_mut();
    let status = unsafe {
        GetSecurityInfo(
            handle.raw(),
            SE_FILE_OBJECT,
            SECURITY_PARTS,
            ptr::null_mut(),
            ptr::null_mut(),
            ptr::null_mut(),
            ptr::null_mut(),
            &mut sd,
        )
    };
    let owned = Local(sd);
    if status != 0 {
        bail!("GetSecurityInfo(network setup) failed: {status}");
    }
    same_security(
        owned.0,
        descriptor(if directory { DIRECTORY_SD } else { FILE_SD })?.0,
    )?;
    Ok(handle)
}

fn install_path() -> Result<PathBuf> {
    // Machine's native 64-bit installation directory, never ProgramFiles or
    // SystemDrive supplied by the caller's environment. Do not ask the shell
    // to expand registry variables inside this elevated installer.
    let mut buffer = vec![0_u16; 32768];
    let mut bytes = (buffer.len() * 2) as u32;
    let mut kind = 0;
    let code = unsafe {
        RegGetValueW(
            HKEY_LOCAL_MACHINE,
            wide(r"SOFTWARE\Microsoft\Windows\CurrentVersion").as_ptr(),
            wide("ProgramFilesDir").as_ptr(),
            RRF_RT_REG_SZ | RRF_RT_REG_EXPAND_SZ | RRF_NOEXPAND | RRF_SUBKEY_WOW6464KEY,
            &mut kind,
            buffer.as_mut_ptr() as *mut c_void,
            &mut bytes,
        )
    };
    if code != 0
        || !matches!(kind, REG_SZ | REG_EXPAND_SZ)
        || bytes < 2
        || bytes % 2 != 0
        || bytes as usize > buffer.len() * 2
    {
        bail!("cannot read fixed machine ProgramFilesDir (error {code})");
    }
    let units = &buffer[..bytes as usize / 2];
    if units.last() != Some(&0) || units[..units.len() - 1].contains(&0) {
        bail!("invalid machine ProgramFilesDir string");
    }
    let value = String::from_utf16(&units[..units.len() - 1])?;
    let mut windows = vec![0_u16; 32768];
    let count = unsafe { GetSystemWindowsDirectoryW(windows.as_mut_ptr(), windows.len() as u32) };
    if count == 0 || count as usize >= windows.len() {
        return Err(winutil::last_error(
            "GetSystemWindowsDirectoryW(network setup)",
        ));
    }
    let folder = expand_program_files(
        &value,
        kind == REG_EXPAND_SZ,
        &String::from_utf16(&windows[..count as usize])?,
    )?;
    Ok(folder.join(FOLDER).join(EXECUTABLE))
}
fn expand_program_files(value: &str, expandable: bool, windows: &str) -> Result<PathBuf> {
    if windows.as_bytes().get(1) != Some(&b':')
        || !winutil::is_normalized_local_absolute(Path::new(windows))
    {
        bail!("invalid OS Windows directory");
    }
    let mut remaining = value;
    let mut expanded = String::new();
    while expandable && remaining.contains('%') {
        let start = remaining.find('%').unwrap();
        expanded.push_str(&remaining[..start]);
        let variable = &remaining[start + 1..];
        let end = variable
            .find('%')
            .ok_or_else(|| anyhow!("unterminated ProgramFilesDir variable"))?;
        match &variable[..end].to_ascii_lowercase()[..] {
            "systemdrive" => expanded.push_str(&windows[..2]),
            "systemroot" | "windir" => expanded.push_str(windows),
            _ => {
                bail!("unsupported ProgramFilesDir variable; caller environment is never expanded")
            }
        }
        remaining = &variable[end + 1..];
    }
    expanded.push_str(remaining);
    let folder = PathBuf::from(expanded);
    if !winutil::is_normalized_local_absolute(&folder) || winutil::is_volume_root(&folder) {
        bail!("Program Files is not a local normalized directory");
    }
    Ok(folder)
}

fn trusted_administrator(sid: PSID) -> Result<bool> {
    if sid.is_null() || unsafe { IsValidSid(sid) } == 0 {
        bail!("invalid installation ancestor trustee");
    }
    Ok(matches!(
        crate::identity::sid_string(sid)?.as_str(),
        "S-1-5-18"
            | "S-1-5-32-544"
            | "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
    ))
}
fn validate_parent_security(sd: PSECURITY_DESCRIPTOR) -> Result<()> {
    if sd.is_null() || unsafe { IsValidSecurityDescriptor(sd) } == 0 {
        bail!("invalid installation ancestor security");
    }
    let mut owner = ptr::null_mut();
    let mut defaulted = 0;
    if unsafe { GetSecurityDescriptorOwner(sd, &mut owner, &mut defaulted) } == 0
        || !trusted_administrator(owner)?
    {
        bail!("service installation ancestor is not administratively owned");
    }
    let mut acl = ptr::null_mut();
    let mut present = 0;
    if unsafe { GetSecurityDescriptorDacl(sd, &mut present, &mut acl, &mut defaulted) } == 0
        || present == 0
        || acl.is_null()
        || unsafe { IsValidAcl(acl) } == 0
    {
        bail!("installation ancestor has no valid access restrictions");
    }
    for i in 0..unsafe { (*acl).AceCount } as u32 {
        let mut raw = ptr::null_mut();
        if unsafe { GetAce(acl, i, &mut raw) } == 0 || raw.is_null() {
            bail!("cannot inspect installation ancestor permissions");
        }
        let header = unsafe { &*(raw as *const ACE_HEADER) };
        if header.AceFlags as u32 & INHERIT_ONLY_ACE != 0 {
            continue;
        }
        if header.AceType == 1 {
            continue;
        } // A deny cannot authorize replacement.
        if header.AceType != 0 || header.AceSize < 16 {
            bail!("unsupported installation ancestor access entry");
        }
        let ace = unsafe { &*(raw as *const ACCESS_ALLOWED_ACE) };
        // Creating a NEW sibling at C:\ is normal and does not permit replacing
        // this existing protected directory. Only destructive/control rights
        // on the actual ancestry matter for a persistent privileged image.
        let replacement = DELETE | WRITE_DAC | WRITE_OWNER | FILE_DELETE_CHILD | GENERIC_ALL;
        if ace.Mask & replacement != 0
            && !trusted_administrator(ptr::addr_of!(ace.SidStart) as PSID)?
        {
            bail!("an unprivileged trustee can replace/control the service installation ancestry");
        }
    }
    Ok(())
}
fn trusted_parents(path: &Path) -> Result<Vec<(PathBuf, Handle)>> {
    let pins = winutil::pin_directory_chain(path, false)?;
    for (_, handle) in &pins {
        let mut sd = ptr::null_mut();
        let status = unsafe {
            GetSecurityInfo(
                handle.raw(),
                SE_FILE_OBJECT,
                OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
                ptr::null_mut(),
                ptr::null_mut(),
                ptr::null_mut(),
                ptr::null_mut(),
                &mut sd,
            )
        };
        let sd = Local(sd);
        if status != 0 {
            bail!("cannot inspect service installation ancestor: {status}");
        }
        validate_parent_security(sd.0)?;
    }
    Ok(pins)
}
fn image_command(path: &Path) -> Result<String> {
    let path = path
        .to_str()
        .ok_or_else(|| anyhow!("service path is not Unicode"))?;
    if path.contains(['"', '\0']) {
        bail!("invalid fixed service path");
    }
    Ok(format!("\"{path}\" --network-service"))
}
fn manager(write: bool) -> Result<Service> {
    let handle = unsafe {
        OpenSCManagerW(
            ptr::null(),
            ptr::null(),
            SC_MANAGER_CONNECT | if write { SC_MANAGER_CREATE_SERVICE } else { 0 },
        )
    };
    if handle == 0 {
        return Err(winutil::last_error("OpenSCManagerW(network setup)"));
    }
    Ok(Service(handle))
}
fn open_service(manager: &Service, write: bool) -> Result<Option<Service>> {
    let access = SERVICE_QUERY_CONFIG
        | SERVICE_QUERY_STATUS
        | READ_CONTROL
        | if write {
            SERVICE_START | SERVICE_STOP | SERVICE_USER_DEFINED_CONTROL | DELETE | WRITE_DAC
        } else {
            0
        };
    let raw = unsafe { OpenServiceW(manager.0, wide(SERVICE_NAME).as_ptr(), access) };
    if raw == 0 {
        let code = unsafe { GetLastError() };
        if code == ERROR_SERVICE_DOES_NOT_EXIST {
            return Ok(None);
        }
        bail!("OpenServiceW(network setup) failed: {code}");
    }
    Ok(Some(Service(raw)))
}
fn validate_service(service: &Service, path: &Path) -> Result<()> {
    let mut required = 0;
    unsafe {
        QueryServiceConfigW(service.0, ptr::null_mut(), 0, &mut required);
    }
    if required == 0 || required > 64 * 1024 {
        bail!("invalid network service configuration size");
    }
    let mut buffer = vec![0usize; (required as usize).div_ceil(mem::size_of::<usize>())];
    if unsafe {
        QueryServiceConfigW(
            service.0,
            buffer.as_mut_ptr() as *mut _,
            required,
            &mut required,
        )
    } == 0
    {
        return Err(winutil::last_error("QueryServiceConfigW"));
    }
    let config = unsafe { &*(buffer.as_ptr() as *const QUERY_SERVICE_CONFIGW) };
    let string = |p| config_string(&buffer, p);
    if config.dwServiceType != SERVICE_WIN32_OWN_PROCESS
        || config.dwStartType != SERVICE_AUTO_START
        || config.dwErrorControl != SERVICE_ERROR_NORMAL
        || string(config.lpBinaryPathName)? != image_command(path)?
        || string(config.lpServiceStartName)? != "LocalSystem"
        || !string(config.lpLoadOrderGroup)?.is_empty()
        || !string(config.lpDependencies)?.is_empty()
    {
        bail!("existing Bello network service has a foreign configuration; no changes made");
    }
    required = 0;
    unsafe {
        QueryServiceObjectSecurity(service.0, SECURITY_PARTS, ptr::null_mut(), 0, &mut required);
    }
    if required == 0 || required > 64 * 1024 {
        bail!("invalid network service security size");
    }
    let mut sd = vec![0usize; (required as usize).div_ceil(mem::size_of::<usize>())];
    if unsafe {
        QueryServiceObjectSecurity(
            service.0,
            SECURITY_PARTS,
            sd.as_mut_ptr() as *mut _,
            required,
            &mut required,
        )
    } == 0
    {
        return Err(winutil::last_error("QueryServiceObjectSecurity"));
    }
    same_security(sd.as_mut_ptr() as *mut _, descriptor(SERVICE_SD)?.0)
}
fn config_string(buffer: &[usize], value: *const u16) -> Result<String> {
    if value.is_null() {
        return Ok(String::new());
    }
    let start = buffer.as_ptr() as usize;
    let end = start + mem::size_of_val(buffer);
    let pointer = value as usize;
    if pointer < start || pointer >= end || pointer % 2 != 0 {
        bail!("SCM returned a string outside its configuration buffer");
    }
    let units = unsafe { std::slice::from_raw_parts(value, (end - pointer) / 2) };
    let size = units
        .iter()
        .position(|v| *v == 0)
        .ok_or_else(|| anyhow!("unterminated SCM configuration string"))?;
    Ok(String::from_utf16(&units[..size])?)
}
fn service_state(service: &Service) -> Result<SERVICE_STATUS_PROCESS> {
    let mut state = unsafe { mem::zeroed() };
    let mut bytes = 0;
    if unsafe {
        QueryServiceStatusEx(
            service.0,
            SC_STATUS_PROCESS_INFO,
            &mut state as *mut _ as *mut u8,
            mem::size_of::<SERVICE_STATUS_PROCESS>() as u32,
            &mut bytes,
        )
    } == 0
    {
        return Err(winutil::last_error("QueryServiceStatusEx"));
    }
    Ok(state)
}
fn wait_state(service: &Service, target: u32) -> Result<()> {
    let deadline = Instant::now() + Duration::from_secs(20);
    loop {
        if service_state(service)?.dwCurrentState == target {
            return Ok(());
        }
        if Instant::now() >= deadline {
            bail!("network service did not reach requested state within 20 seconds");
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}
fn stop_empty(service: &Service) -> Result<()> {
    if service_state(service)?.dwCurrentState == SERVICE_STOPPED {
        return Ok(());
    }
    require_empty()?;
    let before = service_state(service)?;
    if before.dwCurrentState != SERVICE_RUNNING || before.dwProcessId == 0 {
        bail!("network service is not running and cannot enter safe stop preparation");
    }
    let mut status = unsafe { mem::zeroed() };
    // A STOP callback cannot veto a delivered STOP. First request the fixed
    // administrative control that closes admission only while all leases are
    // empty, then verify SCM's actual status before sending the real STOP.
    if unsafe {
        ControlService(
            service.0,
            crate::network_broker::PREPARE_STOP_CONTROL,
            &mut status,
        )
    } == 0
    {
        return Err(winutil::last_error("prepare inactive network service stop"));
    }
    let prepared = service_state(service)?;
    if prepared.dwCurrentState != SERVICE_RUNNING
        || prepared.dwProcessId != before.dwProcessId
        || prepared.dwControlsAccepted & SERVICE_ACCEPT_STOP == 0
    {
        bail!("network service could not quiesce: active/retained leases or a concurrent operation prevent stopping; nothing was stopped");
    }
    if unsafe { ControlService(service.0, SERVICE_CONTROL_STOP, &mut status) } == 0 {
        return Err(winutil::last_error("stop inactive network service"));
    }
    wait_state(service, SERVICE_STOPPED)
}
fn require_empty() -> Result<()> {
    match crate::network_broker::status()?.result {
        BrokerResult::Status{active_leases:0,retained_leases:0}=>Ok(()),
        _=>bail!("network setup cannot change while active or retained sandbox leases exist; finish runs or resolve recovery first"),
    }
}
struct SourceBinary {
    _parents: Vec<(PathBuf, Handle)>,
    file: std::fs::File,
}
impl SourceBinary {
    fn open() -> Result<Self> {
        let path = winutil::verbatim_local_absolute(&std::env::current_exe()?)?;
        let parents = winutil::pin_directory_chain(
            path.parent()
                .ok_or_else(|| anyhow!("source executable has no parent"))?,
            false,
        )?;
        // Keep one no-write/no-delete handle throughout comparison and copy;
        // neither a junction nor a replacement between those steps is used.
        let file = winutil::open_regular_file_read(&path)?;
        Ok(Self {
            _parents: parents,
            file,
        })
    }
}
fn binary_matches(path: &Path, source: &mut SourceBinary) -> Result<bool> {
    let mut installed = winutil::open_regular_file_read(&winutil::verbatim_local_absolute(path)?)?;
    source.file.rewind()?;
    let (mut a, mut b) = ([0u8; 65536], [0u8; 65536]);
    loop {
        let x = installed.read(&mut a)?;
        let y = source.file.read(&mut b)?;
        if x != y || a[..x] != b[..y] {
            return Ok(false);
        }
        if x == 0 {
            return Ok(true);
        }
    }
}
fn copy_binary(path: &Path, source: &mut SourceBinary) -> Result<()> {
    let staging = path.with_extension("exe.new");
    // A completed staging file from a prior crash can be used only when both
    // its exact administrative ACL and bytes match this running executable.
    // Unknown or partial old artifacts are retained for explicit inspection.
    if staging.try_exists()? {
        drop(validate_object(&staging, false)?);
        if !binary_matches(&staging, source)? {
            bail!("an incomplete or different service staging file exists; nothing overwritten");
        }
        return finish_staging(&staging, path);
    }
    let sd = descriptor(FILE_SD)?;
    let attrs = SECURITY_ATTRIBUTES {
        nLength: mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
        lpSecurityDescriptor: sd.0,
        bInheritHandle: 0,
    };
    let handle = Handle::new(
        unsafe {
            CreateFileW(
                wide(&staging).as_ptr(),
                FILE_GENERIC_WRITE | READ_CONTROL,
                0,
                &attrs,
                CREATE_NEW,
                FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT,
                0,
            )
        },
        "create fixed network service staging file",
    )?;
    let identity = winutil::file_identity(&handle)?;
    let mut target = unsafe { std::fs::File::from_raw_handle(handle.into_raw() as *mut c_void) };
    let outcome = (|| -> Result<()> {
        source.file.rewind()?;
        std::io::copy(&mut source.file, &mut target)?;
        target.flush()?;
        target.sync_all()?;
        Ok(())
    })();
    drop(target);
    if let Err(error) = outcome {
        remove_created_staging(&staging, identity).context(format!(
            "staging write failed: {error:#}; cleanup also failed"
        ))?;
        return Err(error);
    }
    let outcome = finish_staging(&staging, path);
    if outcome.is_err() && staging.try_exists()? {
        remove_created_staging(&staging, identity)
            .context("installation failed and its new staging file could not be removed")?;
    }
    outcome
}
fn remove_created_staging(path: &Path, identity: winutil::FileIdentity) -> Result<()> {
    let path = winutil::verbatim_local_absolute(path)?;
    let handle = Handle::new(
        unsafe {
            CreateFileW(
                wide(&path).as_ptr(),
                DELETE | READ_CONTROL,
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                ptr::null(),
                OPEN_EXISTING,
                FILE_FLAG_OPEN_REPARSE_POINT,
                0,
            )
        },
        "open own staging file for cleanup",
    )?;
    winutil::validate_final_path(&handle, &path)?;
    let current = winutil::validate_plain_file_object(&handle, &path)?;
    if current.volume_serial != identity.volume_serial || current.file_index != identity.file_index
    {
        bail!("staging identity changed; refusing deletion");
    }
    let disposition = FILE_DISPOSITION_INFO { DeleteFile: 1 };
    if unsafe {
        SetFileInformationByHandle(
            handle.raw(),
            FileDispositionInfo,
            &disposition as *const _ as *const c_void,
            mem::size_of_val(&disposition) as u32,
        )
    } == 0
    {
        return Err(winutil::last_error("remove own failed staging file"));
    }
    Ok(())
}
fn finish_staging(staging: &Path, path: &Path) -> Result<()> {
    drop(validate_object(staging, false)?);
    if unsafe {
        MoveFileExW(
            wide(staging).as_ptr(),
            wide(path).as_ptr(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )
    } == 0
    {
        return Err(winutil::last_error("install fixed network service binary"));
    }
    drop(validate_object(path, false)?);
    Ok(())
}

/// The pipe client compares this pinned SCM identity to the actual pipe server.
pub fn running_service_identity() -> Result<(u32, PathBuf)> {
    let path = install_path()?;
    let _parents = trusted_parents(path.parent().unwrap().parent().unwrap())?;
    let _directory = validate_object(path.parent().unwrap(), true)?;
    let _binary = validate_object(&path, false)?;
    let manager = manager(false)?;
    let service=open_service(&manager,false)?.ok_or_else(||anyhow!("offline network service is not installed; run bello runtime windows-sandbox prepare --network from an Administrator terminal"))?;
    validate_service(&service, &path)?;
    let state = service_state(&service)?;
    if state.dwCurrentState != SERVICE_RUNNING || state.dwProcessId == 0 {
        bail!("offline network service is not running; explicit administrator preparation is required");
    }
    Ok((state.dwProcessId, path))
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct NetworkPreparationReport {
    protocol_version: u32,
    kind: &'static str,
    operation: String,
    service_name: &'static str,
    install_path: PathBuf,
    installed: bool,
    running: bool,
    quiesced: bool,
    prepared: bool,
    changed: bool,
    service_pid: u32,
    active_leases: usize,
    retained_leases: usize,
    policy_version: u32,
    binary_matches: bool,
}

pub fn execute(operation: &str) -> Result<NetworkPreparationReport> {
    if !["status", "prepare", "remove"].contains(&operation) {
        bail!("unknown fixed network setup operation");
    }
    let write = operation != "status";
    if write {
        crate::host_prepare::require_elevated_admin()?;
    }
    let _lock = if write {
        Some(crate::host_prepare::SetupLock::acquire()?)
    } else {
        None
    };
    let path = install_path()?;
    let directory = path.parent().unwrap();
    let _parents = trusted_parents(directory.parent().unwrap())?;
    let mut source = if operation == "remove" {
        None
    } else {
        Some(SourceBinary::open()?)
    };
    let manager = manager(write)?;
    let mut service = open_service(&manager, write)?;
    let folder_exists = directory.try_exists()?;
    if folder_exists {
        drop(validate_object(directory, true)?);
    }
    let mut changed = false;
    if let Some(ref existing) = service {
        if !folder_exists {
            bail!("network service exists without its trusted directory");
        }
        drop(validate_object(&path, false)?);
        validate_service(existing, &path)?;
    }
    if operation == "prepare" {
        if !folder_exists {
            let sd = descriptor(DIRECTORY_SD)?;
            let attrs = SECURITY_ATTRIBUTES {
                nLength: mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
                lpSecurityDescriptor: sd.0,
                bInheritHandle: 0,
            };
            if unsafe { CreateDirectoryW(wide(directory).as_ptr(), &attrs) } == 0 {
                return Err(winutil::last_error(
                    "create fixed network service directory",
                ));
            }
            drop(validate_object(directory, true)?);
            changed = true;
        }
        let _directory = validate_object(directory, true)?;
        if let Some(ref existing) = service {
            let matches = binary_matches(&path, source.as_mut().unwrap())?;
            let state = service_state(existing)?;
            let quiesced = state.dwCurrentState == SERVICE_RUNNING
                && state.dwControlsAccepted & SERVICE_ACCEPT_STOP != 0;
            if !matches || quiesced {
                stop_empty(existing)?;
                if !crate::offline_network::leases()?.is_empty() {
                    bail!("retained offline leases prevent a service binary update");
                }
                if !matches {
                    copy_binary(&path, source.as_mut().unwrap())?;
                }
                changed = true;
            }
        } else {
            // Never overwrite a binary left by an unrecognized installation.
            if path.try_exists()? {
                drop(validate_object(&path, false)?);
                if !binary_matches(&path, source.as_mut().unwrap())? {
                    bail!("orphan network binary differs; inspect it before preparing");
                }
            } else {
                copy_binary(&path, source.as_mut().unwrap())?;
            }
            let raw = unsafe {
                CreateServiceW(
                    manager.0,
                    wide(SERVICE_NAME).as_ptr(),
                    wide("Bello offline sandbox network isolation").as_ptr(),
                    SERVICE_ALL_ACCESS,
                    SERVICE_WIN32_OWN_PROCESS,
                    SERVICE_AUTO_START,
                    SERVICE_ERROR_NORMAL,
                    wide(image_command(&path)?).as_ptr(),
                    ptr::null(),
                    ptr::null_mut(),
                    ptr::null(),
                    ptr::null(),
                    ptr::null(),
                )
            };
            if raw == 0 {
                return Err(winutil::last_error("CreateServiceW(BelloOfflineNetwork)"));
            }
            let created = Service(raw);
            let secured = (|| -> Result<()> {
                if unsafe {
                    SetServiceObjectSecurity(
                        created.0,
                        SECURITY_PARTS | PROTECTED_DACL_SECURITY_INFORMATION,
                        descriptor(SERVICE_SD)?.0,
                    )
                } == 0
                {
                    return Err(winutil::last_error("protect fixed network service"));
                }
                validate_service(&created, &path)
            })();
            if let Err(error) = secured {
                // This exact handle was just created by us and was never
                // started. Do not strand an unverified service configuration.
                if unsafe { DeleteService(created.0) } == 0 {
                    return Err(
                        error.context("new service protection failed; service cleanup also failed")
                    );
                }
                return Err(error);
            }
            service = Some(created);
            changed = true;
        }
        crate::offline_network::ensure_layout()?;
        let installed = service.as_ref().unwrap();
        if service_state(installed)?.dwCurrentState != SERVICE_RUNNING {
            if unsafe { StartServiceW(installed.0, 0, ptr::null()) } == 0 {
                return Err(winutil::last_error("StartServiceW(BelloOfflineNetwork)"));
            }
            wait_state(installed, SERVICE_RUNNING)?;
            changed = true;
        }
    } else if operation == "remove" {
        if let Some(existing) = service.take() {
            stop_empty(&existing)?;
            crate::offline_network::remove_layout()?;
            if unsafe { DeleteService(existing.0) } == 0 {
                return Err(winutil::last_error("DeleteService(BelloOfflineNetwork)"));
            }
            drop(existing);
            drop(validate_object(&path, false)?);
            if unsafe { DeleteFileW(wide(&path).as_ptr()) } == 0 {
                return Err(winutil::last_error("remove fixed network service binary"));
            }
            if unsafe { RemoveDirectoryW(wide(directory).as_ptr()) } == 0 {
                return Err(winutil::last_error(
                    "remove empty fixed network service directory",
                ));
            }
            changed = true;
        } else if folder_exists {
            bail!("orphan network installation requires inspection; nothing removed");
        }
    }
    let installed = service.is_some();
    let state = service.as_ref().map(service_state).transpose()?;
    let running = state
        .as_ref()
        .is_some_and(|s| s.dwCurrentState == SERVICE_RUNNING);
    let quiesced = running
        && state
            .as_ref()
            .is_some_and(|s| s.dwControlsAccepted & SERVICE_ACCEPT_STOP != 0);
    let matches = installed
        && binary_matches(
            &path,
            source
                .as_mut()
                .ok_or_else(|| anyhow!("missing pinned source executable"))?,
        )?;
    let (mut active, mut retained) = (0, 0);
    if running {
        crate::offline_network::validate_layout()?;
        let reply = crate::network_broker::status()?;
        match reply.result {
            BrokerResult::Status {
                active_leases,
                retained_leases,
            } => {
                active = active_leases;
                retained = retained_leases;
            }
            _ => bail!("unexpected offline network status reply"),
        }
    }
    Ok(NetworkPreparationReport {
        protocol_version: 1,
        kind: "networkPreparation",
        operation: operation.to_owned(),
        service_name: SERVICE_NAME,
        install_path: path,
        installed,
        running,
        quiesced,
        prepared: running && matches && !quiesced,
        changed,
        service_pid: state.map(|s| s.dwProcessId).unwrap_or(0),
        active_leases: active,
        retained_leases: retained,
        policy_version: NETWORK_POLICY_VERSION,
        binary_matches: matches,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn image_path_is_fixed_and_quoted() {
        assert_eq!(
            image_command(Path::new(
                r"C:\Program Files\BelloOfflineNetwork\bello-windows-sandbox.exe"
            ))
            .unwrap(),
            r#""C:\Program Files\BelloOfflineNetwork\bello-windows-sandbox.exe" --network-service"#
        );
        assert!(image_command(Path::new("bad\"path")).is_err());
    }
    #[test]
    fn explicit_security_descriptors_roundtrip() {
        for policy in [FILE_SD, DIRECTORY_SD, SERVICE_SD] {
            let a = descriptor(policy).unwrap();
            let b = descriptor(policy).unwrap();
            same_security(a.0, b.0).unwrap();
        }
        assert!(same_security(
            descriptor(FILE_SD).unwrap().0,
            descriptor(DIRECTORY_SD).unwrap().0
        )
        .is_err());
        assert!(same_security(ptr::null_mut(), descriptor(FILE_SD).unwrap().0).is_err());
        assert!(same_security(descriptor(FILE_SD).unwrap().0, ptr::null_mut()).is_err());
        assert!(same_security(
            descriptor("D:P(A;;FA;;;SY)").unwrap().0,
            descriptor(FILE_SD).unwrap().0
        )
        .is_err());
    }
    #[test]
    fn machine_program_files_expansion_never_uses_caller_environment() {
        assert_eq!(
            expand_program_files(r"%SystemDrive%\Program Files", true, r"C:\Windows").unwrap(),
            PathBuf::from(r"C:\Program Files")
        );
        assert_eq!(
            expand_program_files(r"D:\Programs", false, r"C:\Windows").unwrap(),
            PathBuf::from(r"D:\Programs")
        );
        for bad in [
            r"%ProgramFiles%\Bello",
            r"%TEMP%\Bello",
            r"%SystemDrive",
            r"C:\",
            r"\\server\Programs",
            r"C:\Programs\..\evil",
        ] {
            assert!(
                expand_program_files(bad, true, r"C:\Windows").is_err(),
                "{bad}"
            );
        }
    }
    #[test]
    fn ancestry_allows_new_siblings_but_not_replacement_by_users() {
        // Common C:\ pattern: authenticated users may create new directories,
        // but may not delete/rename this existing administrative path.
        let ordinary = "O:BAG:BAD:(A;;FA;;;SY)(A;;FA;;;BA)(A;;0x00000006;;;AU)(A;OICIIO;FA;;;CO)";
        validate_parent_security(descriptor(ordinary).unwrap().0).unwrap();
        for right in ["0x00000040", "0x00010000", "0x00040000", "0x00080000", "GA"] {
            let sd = descriptor(&format!("O:BAG:BAD:(A;;FA;;;SY)(A;;{right};;;AU)")).unwrap();
            assert!(validate_parent_security(sd.0).is_err(), "{right}");
        }
        assert!(validate_parent_security(descriptor("O:BUG:BUD:(A;;FR;;;BU)").unwrap().0).is_err());
        assert!(validate_parent_security(ptr::null_mut()).is_err());
    }
    #[test]
    fn scm_strings_are_bounded_and_null_safe() {
        let mut storage = [0_usize; 4];
        let pointer = storage.as_mut_ptr() as *mut u16;
        unsafe {
            *pointer = b'x' as u16;
        }
        assert_eq!(config_string(&storage, pointer).unwrap(), "x");
        assert_eq!(config_string(&storage, ptr::null()).unwrap(), "");
        assert!(config_string(&storage, (pointer as usize + 1) as *const u16).is_err());
        let outside = [0_u16; 1];
        assert!(config_string(&storage, outside.as_ptr()).is_err());
        storage.fill(usize::MAX);
        assert!(config_string(&storage, storage.as_ptr() as *const u16).is_err());
    }
}
