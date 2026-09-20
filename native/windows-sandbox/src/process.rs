use crate::identity::CapabilitySids;
use crate::winutil::{
    as_void, checked_usize_to_u32, last_error, verbatim_local_absolute, wide, Handle,
};
use anyhow::{anyhow, Context, Result};
use std::collections::BTreeMap;
use std::ffi::{c_void, OsString};
use std::mem;
use std::os::windows::ffi::OsStringExt;
use std::path::{Component, Path, PathBuf, Prefix};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use windows_sys::Win32::Foundation::{
    GetHandleInformation, GetLastError, SetHandleInformation, HANDLE, HANDLE_FLAG_INHERIT,
    INVALID_HANDLE_VALUE,
};
use windows_sys::Win32::Security::{
    EqualSid, GetTokenInformation, TokenAppContainerSid, TokenIsAppContainer,
    SECURITY_CAPABILITIES, TOKEN_APPCONTAINER_INFORMATION, TOKEN_DUPLICATE,
    TOKEN_INFORMATION_CLASS, TOKEN_QUERY,
};
use windows_sys::Win32::Storage::FileSystem::{
    CreateFileW, ReadFile, FILE_GENERIC_READ, FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE,
    OPEN_EXISTING,
};
use windows_sys::Win32::System::Console::{GetStdHandle, STD_INPUT_HANDLE, STD_OUTPUT_HANDLE};
use windows_sys::Win32::System::JobObjects::{
    CreateJobObjectW, JobObjectBasicAccountingInformation, JobObjectBasicUIRestrictions,
    JobObjectExtendedLimitInformation, QueryInformationJobObject, SetInformationJobObject,
    TerminateJobObject, JOBOBJECT_BASIC_ACCOUNTING_INFORMATION, JOBOBJECT_BASIC_UI_RESTRICTIONS,
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    JOB_OBJECT_UILIMIT_DESKTOP, JOB_OBJECT_UILIMIT_DISPLAYSETTINGS, JOB_OBJECT_UILIMIT_EXITWINDOWS,
    JOB_OBJECT_UILIMIT_GLOBALATOMS, JOB_OBJECT_UILIMIT_HANDLES, JOB_OBJECT_UILIMIT_READCLIPBOARD,
    JOB_OBJECT_UILIMIT_SYSTEMPARAMETERS, JOB_OBJECT_UILIMIT_WRITECLIPBOARD,
};
use windows_sys::Win32::System::SystemInformation::GetSystemDirectoryW;
use windows_sys::Win32::System::Threading::{
    CreateProcessW, DeleteProcThreadAttributeList, GetExitCodeProcess,
    InitializeProcThreadAttributeList, OpenProcessToken, ResumeThread, UpdateProcThreadAttribute,
    WaitForSingleObject, CREATE_NO_WINDOW, CREATE_SUSPENDED, CREATE_UNICODE_ENVIRONMENT,
    EXTENDED_STARTUPINFO_PRESENT, PROCESS_INFORMATION,
    PROC_THREAD_ATTRIBUTE_ALL_APPLICATION_PACKAGES_POLICY, PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
    PROC_THREAD_ATTRIBUTE_JOB_LIST, PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
    STARTF_USESTDHANDLES, STARTUPINFOEXW,
};

const INFINITE: u32 = 0xffff_ffff;
const WAIT_OBJECT_0: u32 = 0;
const WAIT_FAILED: u32 = 0xffff_ffff;
const WAIT_TIMEOUT: u32 = 258;
const JOB_EMPTY_WAIT_MILLIS: u32 = 5_000;
const PROCESS_CREATION_ALL_APPLICATION_PACKAGES_OPT_OUT: u32 = 1;
// stdout is shared across concurrently launched commands and native tests.
// Hold this only while marking handles and creating the suspended process.
static HANDLE_INHERIT_LOCK: Mutex<()> = Mutex::new(());

#[cfg(test)]
thread_local! {
    static TEST_BROKER_UNAVAILABLE: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
}

fn register_offline(
    profile: &str,
    pid: u32,
    job: &Arc<Job>,
) -> Result<crate::network_broker::OfflineLease> {
    #[cfg(test)]
    if TEST_BROKER_UNAVAILABLE.with(std::cell::Cell::get) {
        return Err(anyhow!(
            "test-only simulated missing offline-network service"
        ));
    }
    crate::network_broker::register(profile, pid, job)
}

pub struct Job {
    handle: Handle,
    termination_failures: Mutex<Vec<String>>,
}

unsafe impl Send for Job {}
unsafe impl Sync for Job {}

impl Job {
    pub fn create() -> Result<Arc<Self>> {
        let handle = Handle::new(
            unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) },
            "CreateJobObjectW",
        )?;
        let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { mem::zeroed() };
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if unsafe {
            SetInformationJobObject(
                handle.raw(),
                JobObjectExtendedLimitInformation,
                as_void(&limits),
                checked_usize_to_u32(mem::size_of_val(&limits), "job limit structure")?,
            )
        } == 0
        {
            return Err(last_error("SetInformationJobObject(extended limits)"));
        }

        let restrictions = JOBOBJECT_BASIC_UI_RESTRICTIONS {
            UIRestrictionsClass: JOB_OBJECT_UILIMIT_HANDLES
                | JOB_OBJECT_UILIMIT_READCLIPBOARD
                | JOB_OBJECT_UILIMIT_WRITECLIPBOARD
                | JOB_OBJECT_UILIMIT_SYSTEMPARAMETERS
                | JOB_OBJECT_UILIMIT_DISPLAYSETTINGS
                | JOB_OBJECT_UILIMIT_GLOBALATOMS
                | JOB_OBJECT_UILIMIT_DESKTOP
                | JOB_OBJECT_UILIMIT_EXITWINDOWS,
        };
        if unsafe {
            SetInformationJobObject(
                handle.raw(),
                JobObjectBasicUIRestrictions,
                as_void(&restrictions),
                checked_usize_to_u32(mem::size_of_val(&restrictions), "job UI structure")?,
            )
        } == 0
        {
            return Err(last_error("SetInformationJobObject(UI restrictions)"));
        }
        Ok(Arc::new(Self {
            handle,
            termination_failures: Mutex::new(Vec::new()),
        }))
    }

    pub fn raw(&self) -> HANDLE {
        self.handle.raw()
    }

    fn record_failure(&self, error: &anyhow::Error) {
        if let Ok(mut failures) = self.termination_failures.lock() {
            let message = format!("{error:#}");
            if !failures.contains(&message) {
                failures.push(message);
            }
        }
    }

    pub fn failure_messages(&self) -> Vec<String> {
        self.termination_failures
            .lock()
            .map(|failures| failures.clone())
            .unwrap_or_else(|_| vec!["job termination failure lock was poisoned".to_owned()])
    }

    pub fn ensure_empty(&self) -> Result<()> {
        let mut accounting: JOBOBJECT_BASIC_ACCOUNTING_INFORMATION = unsafe { mem::zeroed() };
        if unsafe {
            QueryInformationJobObject(
                self.raw(),
                JobObjectBasicAccountingInformation,
                &mut accounting as *mut _ as *mut c_void,
                checked_usize_to_u32(mem::size_of_val(&accounting), "job accounting structure")?,
                std::ptr::null_mut(),
            )
        } == 0
        {
            return Err(last_error("QueryInformationJobObject(accounting)"));
        }
        if accounting.ActiveProcesses != 0 {
            return Err(anyhow!(
                "Windows sandbox job still has {} active process(es)",
                accounting.ActiveProcesses
            ));
        }
        Ok(())
    }

    pub fn terminate(&self, exit_code: u32) -> Result<()> {
        let result = (|| {
            if unsafe { TerminateJobObject(self.raw(), exit_code) } == 0 {
                return Err(last_error("TerminateJobObject"));
            }
            let wait = unsafe { WaitForSingleObject(self.raw(), JOB_EMPTY_WAIT_MILLIS) };
            match wait {
                WAIT_OBJECT_0 => {}
                WAIT_TIMEOUT => {
                    return Err(anyhow!(
                        "Windows sandbox job did not empty within {JOB_EMPTY_WAIT_MILLIS} ms"
                    ));
                }
                WAIT_FAILED => return Err(last_error("WaitForSingleObject(job)")),
                other => return Err(anyhow!("unexpected job wait result {other:#x}")),
            }
            self.ensure_empty()
        })();
        if let Err(error) = &result {
            self.record_failure(error);
        }
        result
    }
}

pub fn start_parent_monitor(job: Arc<Job>, cancelled: Arc<AtomicBool>) -> Result<()> {
    let stdin = unsafe { GetStdHandle(STD_INPUT_HANDLE) };
    if stdin == 0 || stdin == INVALID_HANDLE_VALUE {
        return Err(last_error("GetStdHandle(STD_INPUT_HANDLE)"));
    }
    std::thread::Builder::new()
        .name("bello-parent-monitor".to_owned())
        .spawn(move || {
            let mut byte = [0_u8; 1];
            let mut read = 0_u32;
            // No bytes are valid after the request frame. EOF, a broken pipe,
            // or unexpected input all mean the trusted controller is gone or
            // the protocol has been violated; every case fails closed.
            unsafe {
                ReadFile(stdin, byte.as_mut_ptr(), 1, &mut read, std::ptr::null_mut());
            }
            cancelled.store(true, Ordering::Release);
            let _ = job.terminate(130);
        })
        .context("could not start parent-death monitor")?;
    Ok(())
}

struct AttributeList {
    storage: Vec<usize>,
    initialized: bool,
}

impl AttributeList {
    fn new(count: u32) -> Result<Self> {
        let mut bytes = 0_usize;
        unsafe {
            InitializeProcThreadAttributeList(std::ptr::null_mut(), count, 0, &mut bytes);
        }
        if bytes == 0 {
            return Err(last_error("InitializeProcThreadAttributeList(size)"));
        }
        let words = bytes.div_ceil(mem::size_of::<usize>());
        let mut value = Self {
            storage: vec![0_usize; words],
            initialized: false,
        };
        if unsafe { InitializeProcThreadAttributeList(value.ptr(), count, 0, &mut bytes) } == 0 {
            return Err(last_error("InitializeProcThreadAttributeList"));
        }
        value.initialized = true;
        Ok(value)
    }

    fn ptr(&mut self) -> *mut c_void {
        self.storage.as_mut_ptr() as *mut c_void
    }

    fn set<T>(&mut self, attribute: u32, value: &T) -> Result<()> {
        if unsafe {
            UpdateProcThreadAttribute(
                self.ptr(),
                0,
                attribute as usize,
                as_void(value),
                mem::size_of::<T>(),
                std::ptr::null_mut(),
                std::ptr::null(),
            )
        } == 0
        {
            return Err(last_error(&format!(
                "UpdateProcThreadAttribute({attribute:#x})"
            )));
        }
        Ok(())
    }

    fn set_slice<T>(&mut self, attribute: u32, values: &[T]) -> Result<()> {
        if values.is_empty() {
            return Err(anyhow!("attribute {attribute:#x} cannot be an empty slice"));
        }
        if unsafe {
            UpdateProcThreadAttribute(
                self.ptr(),
                0,
                attribute as usize,
                values.as_ptr() as *const c_void,
                mem::size_of_val(values),
                std::ptr::null_mut(),
                std::ptr::null(),
            )
        } == 0
        {
            return Err(last_error(&format!(
                "UpdateProcThreadAttribute({attribute:#x})"
            )));
        }
        Ok(())
    }
}

impl Drop for AttributeList {
    fn drop(&mut self) {
        if self.initialized {
            unsafe {
                DeleteProcThreadAttributeList(self.ptr());
            }
        }
    }
}

fn nul_handle() -> Result<Handle> {
    let handle = unsafe {
        CreateFileW(
            wide("NUL").as_ptr(),
            FILE_GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            std::ptr::null(),
            OPEN_EXISTING,
            0,
            0,
        )
    };
    Handle::new(handle, "CreateFileW(NUL)")
}

fn set_inheritable(handle: HANDLE, enabled: bool) -> Result<()> {
    if unsafe {
        SetHandleInformation(
            handle,
            HANDLE_FLAG_INHERIT,
            if enabled { HANDLE_FLAG_INHERIT } else { 0 },
        )
    } == 0
    {
        return Err(last_error("SetHandleInformation"));
    }
    Ok(())
}

struct InheritGuard {
    handle: HANDLE,
    was_inheritable: bool,
}

impl InheritGuard {
    fn new(handle: HANDLE) -> Result<Self> {
        let mut flags = 0;
        if unsafe { GetHandleInformation(handle, &mut flags) } == 0 {
            return Err(last_error("GetHandleInformation"));
        }
        set_inheritable(handle, true)?;
        Ok(Self {
            handle,
            was_inheritable: flags & HANDLE_FLAG_INHERIT != 0,
        })
    }
}

impl Drop for InheritGuard {
    fn drop(&mut self) {
        let _ = set_inheritable(self.handle, self.was_inheritable);
    }
}

fn quote_windows_argument(value: &str) -> String {
    if !value.is_empty()
        && !value
            .chars()
            .any(|character| character == ' ' || character == '\t' || character == '"')
    {
        return value.to_owned();
    }
    let mut quoted = String::from("\"");
    let mut backslashes = 0_usize;
    for character in value.chars() {
        if character == '\\' {
            backslashes += 1;
        } else if character == '"' {
            quoted.push_str(&"\\".repeat(backslashes * 2 + 1));
            quoted.push('"');
            backslashes = 0;
        } else {
            quoted.push_str(&"\\".repeat(backslashes));
            backslashes = 0;
            quoted.push(character);
        }
    }
    quoted.push_str(&"\\".repeat(backslashes * 2));
    quoted.push('"');
    quoted
}

fn command_processor_line(shell: &Path, command: &str) -> String {
    // `/s /c` removes this canonical first/last quote pair before interpreting
    // the payload.  The payload itself is copied byte-for-UTF-16-unit: a
    // leading quoted executable, metacharacters, and trailing quotes remain
    // part of the command language rather than MSVCRT arguments.
    format!(
        "{} /d /s /c \"{}\"",
        quote_windows_argument(&shell.display().to_string()),
        command
    )
}

fn system_directory() -> Result<PathBuf> {
    let needed = unsafe { GetSystemDirectoryW(std::ptr::null_mut(), 0) };
    if needed == 0 {
        return Err(last_error("GetSystemDirectoryW(size)"));
    }
    let mut value = vec![0_u16; needed as usize + 1];
    let written = unsafe { GetSystemDirectoryW(value.as_mut_ptr(), value.len() as u32) };
    if written == 0 || written as usize >= value.len() {
        return Err(last_error("GetSystemDirectoryW"));
    }
    value.truncate(written as usize);
    let path = PathBuf::from(std::ffi::OsString::from_wide(&value));
    std::fs::canonicalize(&path)
        .with_context(|| format!("could not canonicalize system directory {}", path.display()))
}

fn drive_name(path: &Path) -> Result<String> {
    let Some(Component::Prefix(prefix)) = path.components().next() else {
        return Err(anyhow!("system directory has no drive prefix"));
    };
    let drive = match prefix.kind() {
        Prefix::Disk(value) | Prefix::VerbatimDisk(value) => value,
        _ => return Err(anyhow!("system directory is not on a local drive")),
    };
    Ok(format!("{}:", drive as char))
}

fn environment_block(values: &BTreeMap<String, String>) -> Result<Vec<u16>> {
    let mut block = Vec::new();
    for (name, value) in values {
        if name.contains(['=', '\0']) || value.contains('\0') {
            return Err(anyhow!("invalid character in child environment"));
        }
        block.extend(format!("{name}={value}").encode_utf16());
        block.push(0);
    }
    block.push(0);
    Ok(block)
}

pub fn clean_environment(
    profile_local: &Path,
    root: &Path,
    readable_roots: &[PathBuf],
) -> Result<Vec<u16>> {
    let system32 = system_directory()?;
    let system_root = system32
        .parent()
        .ok_or_else(|| anyhow!("system directory has no Windows parent"))?
        .to_owned();
    let temp = profile_local.join("Temp");
    let mut path_entries = vec![system32.clone(), system_root.clone(), root.to_owned()];
    for relative in [
        PathBuf::from(".venv").join("Scripts"),
        PathBuf::from("venv").join("Scripts"),
        PathBuf::from("node_modules").join(".bin"),
    ] {
        let candidate = root.join(relative);
        if candidate.is_dir() {
            path_entries.push(candidate);
        }
    }
    for authority in readable_roots {
        if authority.is_dir() {
            path_entries.push(authority.clone());
            for child in ["Scripts", "bin", "cmd"] {
                let candidate = authority.join(child);
                if candidate.is_dir() {
                    path_entries.push(candidate);
                }
            }
        }
    }
    path_entries.dedup();
    let path = path_entries
        .iter()
        .map(|entry| entry.as_os_str().to_string_lossy())
        .collect::<Vec<_>>()
        .join(";");
    let profile = profile_local.as_os_str().to_string_lossy().into_owned();
    let temp = temp.as_os_str().to_string_lossy().into_owned();
    let mut environment = BTreeMap::new();
    environment.insert(
        "COMSPEC".to_owned(),
        system32.join("cmd.exe").display().to_string(),
    );
    environment.insert("HOME".to_owned(), profile.clone());
    environment.insert("LOCALAPPDATA".to_owned(), profile.clone());
    environment.insert("PATH".to_owned(), path);
    environment.insert("PATHEXT".to_owned(), ".COM;.EXE;.BAT;.CMD".to_owned());
    environment.insert("SystemDrive".to_owned(), drive_name(&system_root)?);
    environment.insert("SystemRoot".to_owned(), system_root.display().to_string());
    environment.insert("TEMP".to_owned(), temp.clone());
    environment.insert("TMP".to_owned(), temp);
    environment.insert("USERPROFILE".to_owned(), profile);
    environment.insert("WINDIR".to_owned(), system_root.display().to_string());
    environment_block(&environment)
}

fn command_processor_cwd(cwd: &Path) -> Result<PathBuf> {
    // CMD treats an extended-length cwd as UNC and silently changes directory.
    // Only strip the prefix after strict local-path validation, so names such
    // as trailing-dot/space components cannot acquire different DOS meanings.
    let extended = verbatim_local_absolute(cwd)?;
    let units = wide(&extended);
    Ok(PathBuf::from(OsString::from_wide(
        &units[4..units.len() - 1],
    )))
}

fn token_flag(token: HANDLE, class: TOKEN_INFORMATION_CLASS, label: &str) -> Result<u32> {
    let mut value = 0_u32;
    let mut returned = 0_u32;
    if unsafe {
        GetTokenInformation(
            token,
            class,
            &mut value as *mut _ as *mut c_void,
            mem::size_of_val(&value) as u32,
            &mut returned,
        )
    } == 0
    {
        return Err(last_error(&format!("GetTokenInformation({label})")));
    }
    if returned != mem::size_of_val(&value) as u32 {
        return Err(anyhow!("invalid {label} token information length"));
    }
    Ok(value)
}

fn verify_child_token(process: HANDLE, expected_sid: *mut c_void) -> Result<Handle> {
    let mut raw_token = 0;
    if unsafe { OpenProcessToken(process, TOKEN_QUERY | TOKEN_DUPLICATE, &mut raw_token) } == 0 {
        return Err(last_error("OpenProcessToken(sandbox child)"));
    }
    let token = Handle::new(raw_token, "OpenProcessToken(sandbox child)")?;
    if token_flag(token.raw(), TokenIsAppContainer, "TokenIsAppContainer")? != 1 {
        return Err(anyhow!(
            "sandbox child token is not AppContainer; command not resumed"
        ));
    }
    let mut required = 0_u32;
    unsafe {
        GetTokenInformation(
            token.raw(),
            TokenAppContainerSid,
            std::ptr::null_mut(),
            0,
            &mut required,
        );
    }
    if required < mem::size_of::<TOKEN_APPCONTAINER_INFORMATION>() as u32 {
        return Err(anyhow!(
            "invalid sandbox child AppContainer SID information length"
        ));
    }
    let mut buffer = vec![0_usize; (required as usize).div_ceil(mem::size_of::<usize>())];
    if unsafe {
        GetTokenInformation(
            token.raw(),
            TokenAppContainerSid,
            buffer.as_mut_ptr() as *mut c_void,
            required,
            &mut required,
        )
    } == 0
    {
        return Err(last_error("GetTokenInformation(TokenAppContainerSid)"));
    }
    let actual_sid =
        unsafe { (*(buffer.as_ptr() as *const TOKEN_APPCONTAINER_INFORMATION)).TokenAppContainer };
    if actual_sid.is_null()
        || expected_sid.is_null()
        || unsafe { EqualSid(actual_sid, expected_sid) } == 0
    {
        return Err(anyhow!(
            "sandbox child AppContainer SID does not match the run; command not resumed"
        ));
    }
    // GetTokenInformation rejects TokenIsLessPrivilegedAppContainer on the
    // supported Windows Server versions. LPAC is requested by the checked
    // ALL_APPLICATION_PACKAGES_POLICY attribute; native tests additionally
    // prove that ALL_APPLICATION_PACKAGES alone does not grant file access.
    Ok(token)
}

#[cfg(test)]
pub fn run_child(
    command: &str,
    cwd: &Path,
    appcontainer_sid: *mut c_void,
    capabilities: &mut CapabilitySids,
    environment: &mut [u16],
    job: &Arc<Job>,
    cancelled: &Arc<AtomicBool>,
) -> Result<i32> {
    run_child_verified(
        command,
        cwd,
        appcontainer_sid,
        capabilities,
        environment,
        job,
        cancelled,
        &mut |_| Ok(()),
    )
}

/// Verify the token identity and filesystem policy before any child code runs.
#[allow(clippy::too_many_arguments)]
pub fn run_child_verified(
    command: &str,
    cwd: &Path,
    appcontainer_sid: *mut c_void,
    capabilities: &mut CapabilitySids,
    environment: &mut [u16],
    job: &Arc<Job>,
    cancelled: &Arc<AtomicBool>,
    verify_access: &mut dyn FnMut(&Handle) -> Result<()>,
) -> Result<i32> {
    run_child_inner(
        command,
        cwd,
        appcontainer_sid,
        capabilities,
        environment,
        job,
        cancelled,
        verify_access,
        None,
    )
}

/// Socket creation is allowed, but the broker installs persistent per-package
/// traffic denies before the suspended child is resumed.
#[allow(clippy::too_many_arguments)]
pub fn run_child_offline(
    command: &str,
    cwd: &Path,
    appcontainer_sid: *mut c_void,
    capabilities: &mut CapabilitySids,
    environment: &mut [u16],
    job: &Arc<Job>,
    cancelled: &Arc<AtomicBool>,
    verify_access: &mut dyn FnMut(&Handle) -> Result<()>,
    profile_name: &str,
) -> Result<i32> {
    run_child_inner(
        command,
        cwd,
        appcontainer_sid,
        capabilities,
        environment,
        job,
        cancelled,
        verify_access,
        Some(profile_name),
    )
}

#[allow(clippy::too_many_arguments)]
fn run_child_inner(
    command: &str,
    cwd: &Path,
    appcontainer_sid: *mut c_void,
    capabilities: &mut CapabilitySids,
    environment: &mut [u16],
    job: &Arc<Job>,
    cancelled: &Arc<AtomicBool>,
    verify_access: &mut dyn FnMut(&Handle) -> Result<()>,
    offline_profile: Option<&str>,
) -> Result<i32> {
    if cancelled.load(Ordering::Acquire) {
        return Err(anyhow!(
            "sandbox launch cancelled because the controller closed stdin"
        ));
    }
    let shell = system_directory()?.join("cmd.exe");
    let shell = std::fs::canonicalize(&shell).with_context(|| {
        format!(
            "system command processor is unavailable at {}",
            shell.display()
        )
    })?;
    let raw_command_line = command_processor_line(&shell, command);
    let mut command_line: Vec<u16> = raw_command_line.encode_utf16().chain(Some(0)).collect();
    if command_line.len() > 32767 {
        return Err(anyhow!(
            "Windows command line exceeds 32767 UTF-16 code units"
        ));
    }
    let process_cwd = command_processor_cwd(cwd)?;

    let stdin = nul_handle()?;
    let stdout = unsafe { GetStdHandle(STD_OUTPUT_HANDLE) };
    if stdout == 0 || stdout == INVALID_HANDLE_VALUE {
        return Err(last_error("GetStdHandle(STD_OUTPUT_HANDLE)"));
    }
    let inherit_lock = HANDLE_INHERIT_LOCK
        .lock()
        .map_err(|_| anyhow!("sandbox handle inheritance lock was poisoned"))?;
    let stdin_inherit = InheritGuard::new(stdin.raw())?;
    let stdout_inherit = InheritGuard::new(stdout)?;

    let security_capabilities: SECURITY_CAPABILITIES =
        capabilities.security_capabilities(appcontainer_sid);
    let lpac_policy = PROCESS_CREATION_ALL_APPLICATION_PACKAGES_OPT_OUT;
    let inherited_handles = [stdin.raw(), stdout];
    let jobs = [job.raw()];
    let mut attributes = AttributeList::new(4)?;
    attributes.set(
        PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
        &security_capabilities,
    )?;
    attributes.set(
        PROC_THREAD_ATTRIBUTE_ALL_APPLICATION_PACKAGES_POLICY,
        &lpac_policy,
    )?;
    attributes.set_slice(PROC_THREAD_ATTRIBUTE_HANDLE_LIST, &inherited_handles)?;
    attributes.set_slice(PROC_THREAD_ATTRIBUTE_JOB_LIST, &jobs)?;
    let mut startup: STARTUPINFOEXW = unsafe { mem::zeroed() };
    startup.StartupInfo.cb = mem::size_of::<STARTUPINFOEXW>() as u32;
    startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    startup.StartupInfo.hStdInput = stdin.raw();
    startup.StartupInfo.hStdOutput = stdout;
    startup.StartupInfo.hStdError = stdout;
    startup.lpAttributeList = attributes.ptr();
    let mut process: PROCESS_INFORMATION = unsafe { mem::zeroed() };
    let created = unsafe {
        CreateProcessW(
            wide(&shell).as_ptr(),
            command_line.as_mut_ptr(),
            std::ptr::null(),
            std::ptr::null(),
            1,
            CREATE_SUSPENDED
                | CREATE_NO_WINDOW
                | CREATE_UNICODE_ENVIRONMENT
                | EXTENDED_STARTUPINFO_PRESENT,
            environment.as_ptr() as *const c_void,
            wide(&process_cwd).as_ptr(),
            &startup.StartupInfo,
            &mut process,
        )
    };
    let create_error = unsafe { GetLastError() };
    drop(stdout_inherit);
    drop(stdin_inherit);
    drop(inherit_lock);
    if created == 0 {
        return Err(anyhow!(
            "CreateProcessW(AppContainer) failed with Win32 error {create_error}"
        ));
    }
    let process_handle = Handle(process.hProcess);
    let thread_handle = Handle(process.hThread);
    let verification =
        verify_child_token(process_handle.raw(), appcontainer_sid).and_then(|token| {
            verify_access(&token)
                .context("sandbox filesystem access verification failed; command not resumed")
        });
    if let Err(error) = verification {
        let _ = job.terminate(125);
        return Err(error);
    }
    let mut offline_lease = match offline_profile {
        Some(profile) => match register_offline(profile, process.dwProcessId, job) {
            Ok(lease) => Some(lease),
            Err(error) => {
                let _ = job.terminate(125);
                return Err(
                    error.context("offline-network protection unavailable; command not resumed")
                );
            }
        },
        None => None,
    };
    if cancelled.load(Ordering::Acquire) {
        job.terminate(130)?;
    } else if unsafe { ResumeThread(thread_handle.raw()) } == u32::MAX {
        let error = last_error("ResumeThread");
        let _ = job.terminate(125);
        return Err(error);
    }
    let wait = unsafe { WaitForSingleObject(process_handle.raw(), INFINITE) };
    if wait == WAIT_FAILED {
        let error = last_error("WaitForSingleObject");
        let _ = job.terminate(125);
        return Err(error);
    }
    if wait != WAIT_OBJECT_0 {
        let _ = job.terminate(125);
        return Err(anyhow!("unexpected WaitForSingleObject result {wait:#x}"));
    }
    let mut exit_code = 0_u32;
    if unsafe { GetExitCodeProcess(process_handle.raw(), &mut exit_code) } == 0 {
        let error = last_error("GetExitCodeProcess");
        let _ = job.terminate(125);
        return Err(error);
    }
    // A shell can return after starting a background descendant. Terminating
    // the job here guarantees no descendant survives past ACL revocation.
    job.terminate(exit_code)?;
    if let Some(lease) = &mut offline_lease {
        lease.release().context(
            "command completed but offline-network lease cleanup failed; protection retained",
        )?;
    }
    Ok(exit_code as i32)
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    pub(crate) fn simulate_missing_broker<T>(run: impl FnOnce() -> T) -> T {
        struct Reset;
        impl Drop for Reset {
            fn drop(&mut self) {
                TEST_BROKER_UNAVAILABLE.with(|flag| flag.set(false));
            }
        }
        TEST_BROKER_UNAVAILABLE.with(|flag| flag.set(true));
        let _reset = Reset;
        run()
    }

    pub(crate) struct SuspendedBrokerChild {
        pub profile: String,
        pub package: String,
        pub pid: u32,
        pub token: Handle,
        pub job: Arc<Job>,
        _process: Handle,
        _thread: Handle,
    }
    impl Drop for SuspendedBrokerChild {
        fn drop(&mut self) {
            let _ = self.job.terminate(125);
            let _ = crate::identity::delete_profile(&self.profile);
        }
    }
    pub(crate) fn suspended_broker_child(lpac: bool) -> SuspendedBrokerChild {
        let profile = crate::identity::random_profile_name().unwrap();
        let mut capabilities = CapabilitySids::for_network(true).unwrap();
        let sid = crate::identity::create_profile(&profile, &capabilities).unwrap();
        let job = Job::create().unwrap();
        let security = capabilities.security_capabilities(sid.0);
        let policy = u32::from(lpac);
        let jobs = [job.raw()];
        let mut attributes = AttributeList::new(3).unwrap();
        attributes
            .set(PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES, &security)
            .unwrap();
        attributes
            .set(
                PROC_THREAD_ATTRIBUTE_ALL_APPLICATION_PACKAGES_POLICY,
                &policy,
            )
            .unwrap();
        attributes
            .set_slice(PROC_THREAD_ATTRIBUTE_JOB_LIST, &jobs)
            .unwrap();
        let mut startup: STARTUPINFOEXW = unsafe { mem::zeroed() };
        startup.StartupInfo.cb = mem::size_of::<STARTUPINFOEXW>() as u32;
        startup.lpAttributeList = attributes.ptr();
        let shell = system_directory().unwrap().join("cmd.exe");
        let mut command = wide(command_processor_line(&shell, "exit /b 0"));
        let mut info: PROCESS_INFORMATION = unsafe { mem::zeroed() };
        assert_ne!(
            unsafe {
                CreateProcessW(
                    wide(&shell).as_ptr(),
                    command.as_mut_ptr(),
                    std::ptr::null(),
                    std::ptr::null(),
                    0,
                    CREATE_SUSPENDED | CREATE_NO_WINDOW | EXTENDED_STARTUPINFO_PRESENT,
                    std::ptr::null(),
                    std::ptr::null(),
                    &startup.StartupInfo,
                    &mut info,
                )
            },
            0,
            "{}",
            last_error("test suspended child")
        );
        let child = Handle(info.hProcess);
        let thread = Handle(info.hThread);
        let token = verify_child_token(child.raw(), sid.0).unwrap();
        SuspendedBrokerChild {
            profile,
            package: crate::identity::sid_string(sid.0).unwrap(),
            pid: info.dwProcessId,
            token,
            job,
            _process: child,
            _thread: thread,
        }
    }

    #[test]
    fn broker_lpac_access_control_distinguishes_actual_ac_and_lpac_tokens() {
        for lpac in [false, true] {
            let child = suspended_broker_child(lpac);
            let result =
                crate::network_broker::validate_lpac_restriction(&child.token, &child.package);
            assert_eq!(result.is_ok(), lpac, "LPAC={lpac}: {result:?}");
        }
    }

    #[test]
    fn inheritance_guard_restores_both_original_flag_states() {
        let handle = nul_handle().unwrap();
        for inherited in [false, true] {
            set_inheritable(handle.raw(), inherited).unwrap();
            {
                let _guard = InheritGuard::new(handle.raw()).unwrap();
                let mut flags = 0;
                assert_ne!(unsafe { GetHandleInformation(handle.raw(), &mut flags) }, 0);
                assert_ne!(flags & HANDLE_FLAG_INHERIT, 0);
            }
            let mut flags = 0;
            assert_ne!(unsafe { GetHandleInformation(handle.raw(), &mut flags) }, 0);
            assert_eq!(flags & HANDLE_FLAG_INHERIT != 0, inherited);
        }
    }

    #[test]
    fn ordinary_process_token_is_rejected_before_sandbox_resume() {
        let sid =
            crate::identity::derive_profile_sid(&crate::identity::random_profile_name().unwrap())
                .unwrap();
        let error = verify_child_token(
            unsafe { windows_sys::Win32::System::Threading::GetCurrentProcess() },
            sid.0,
        )
        .unwrap_err();
        assert!(error.to_string().contains("not AppContainer"));
    }

    #[test]
    fn command_processor_cwd_preserves_local_path_and_utf16() {
        assert_eq!(
            command_processor_cwd(Path::new(r"\\?\C:\workspace\α β")).unwrap(),
            Path::new(r"C:\workspace\α β"),
        );
        let units = [67, 58, 92, 120, 0xD800];
        let path = PathBuf::from(OsString::from_wide(&units));
        assert_eq!(command_processor_cwd(&path).unwrap(), path);
        for path in [
            r"\\?\C:\workspace\private.",
            r"\\?\C:\workspace\private ",
            r"\\server\share",
        ] {
            assert!(command_processor_cwd(Path::new(path)).is_err());
        }
    }

    #[test]
    fn command_processor_writes_in_canonical_workspace_cwd() {
        use std::process::Command;
        use std::time::{SystemTime, UNIX_EPOCH};
        let root = std::env::temp_dir().join(format!(
            "bello-cmd-cwd-{}-{} α β",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos(),
        ));
        std::fs::create_dir(&root).unwrap();
        let canonical = std::fs::canonicalize(&root).unwrap();
        let output = Command::new(system_directory().unwrap().join("cmd.exe"))
            .args(["/d", "/s", "/c", "echo CWD_OK>allowed.txt"])
            .current_dir(command_processor_cwd(&canonical).unwrap())
            .output()
            .unwrap();
        assert!(output.status.success(), "{output:?}");
        assert_eq!(
            std::fs::read_to_string(root.join("allowed.txt"))
                .unwrap()
                .trim(),
            "CWD_OK"
        );
        std::fs::remove_file(root.join("allowed.txt")).unwrap();
        std::fs::remove_dir(root).unwrap();
    }

    #[test]
    fn windows_argument_quoting_preserves_quotes_and_trailing_slashes() {
        assert_eq!(quote_windows_argument("plain"), "plain");
        assert_eq!(quote_windows_argument("two words"), "\"two words\"");
        assert_eq!(quote_windows_argument(r#"a\"b\\"#), r#""a\\\"b\\\\""#);
    }

    #[test]
    fn windows_argument_quoting_round_trips_through_native_parser() {
        use windows_sys::Win32::Foundation::LocalFree;
        use windows_sys::Win32::UI::Shell::CommandLineToArgvW;

        let arguments = [
            "program.exe",
            "",
            "plain",
            "two words",
            "tab\tseparated",
            r#"a\"b\\"#,
            r"C:\Program Files\Tool\",
            r#""quoted""#,
            "α β",
        ];
        let command_line = arguments
            .iter()
            .map(|argument| quote_windows_argument(argument))
            .collect::<Vec<_>>()
            .join(" ");
        let command_line: Vec<_> = command_line.encode_utf16().chain(Some(0)).collect();
        let mut count = 0;
        let parsed = unsafe { CommandLineToArgvW(command_line.as_ptr(), &mut count) };
        assert!(!parsed.is_null(), "CommandLineToArgvW failed");
        // CommandLineToArgvW owns one allocation containing count terminated strings.
        let actual: Vec<_> = unsafe { std::slice::from_raw_parts(parsed, count as usize) }
            .iter()
            .map(|argument| {
                let mut length = 0;
                while unsafe { *argument.add(length) } != 0 {
                    length += 1;
                }
                String::from_utf16_lossy(unsafe { std::slice::from_raw_parts(*argument, length) })
            })
            .collect();
        unsafe { LocalFree(parsed as *mut c_void) };
        assert_eq!(actual, arguments);
    }

    #[test]
    fn command_processor_payload_gets_only_the_cmd_outer_quote_pair() {
        let shell = Path::new(r"C:\Windows\System32\cmd.exe");
        assert_eq!(
            command_processor_line(
                shell,
                r#""C:\Program Files\Tool\tool.exe" "α β" & (echo x|find "x")"#
            ),
            r#"C:\Windows\System32\cmd.exe /d /s /c ""C:\Program Files\Tool\tool.exe" "α β" & (echo x|find "x")""#
        );
        assert_eq!(
            command_processor_line(shell, r#"echo "trailing quote\""#),
            r#"C:\Windows\System32\cmd.exe /d /s /c "echo "trailing quote\"""#
        );
    }
}
