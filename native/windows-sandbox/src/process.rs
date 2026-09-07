use crate::identity::CapabilitySids;
use crate::winutil::{as_void, checked_usize_to_u32, last_error, wide, Handle};
use anyhow::{anyhow, Context, Result};
use std::collections::BTreeMap;
use std::ffi::c_void;
use std::mem;
use std::os::windows::ffi::OsStringExt;
use std::path::{Component, Path, PathBuf, Prefix};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use windows_sys::Win32::Foundation::{
    GetLastError, SetHandleInformation, HANDLE, HANDLE_FLAG_INHERIT, INVALID_HANDLE_VALUE,
};
use windows_sys::Win32::Security::SECURITY_CAPABILITIES;
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
    InitializeProcThreadAttributeList, ResumeThread, UpdateProcThreadAttribute,
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

struct InheritGuard(HANDLE);

impl InheritGuard {
    fn new(handle: HANDLE) -> Result<Self> {
        set_inheritable(handle, true)?;
        Ok(Self(handle))
    }
}

impl Drop for InheritGuard {
    fn drop(&mut self) {
        let _ = set_inheritable(self.0, false);
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

pub fn run_child(
    command: &str,
    cwd: &Path,
    appcontainer_sid: *mut c_void,
    capabilities: &mut CapabilitySids,
    environment: &mut [u16],
    job: &Arc<Job>,
    cancelled: &Arc<AtomicBool>,
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

    let stdin = nul_handle()?;
    let stdout = unsafe { GetStdHandle(STD_OUTPUT_HANDLE) };
    if stdout == 0 || stdout == INVALID_HANDLE_VALUE {
        return Err(last_error("GetStdHandle(STD_OUTPUT_HANDLE)"));
    }
    let _stdin_inherit = InheritGuard::new(stdin.raw())?;
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
            wide(cwd).as_ptr(),
            &startup.StartupInfo,
            &mut process,
        )
    };
    let create_error = unsafe { GetLastError() };
    drop(stdout_inherit);
    if created == 0 {
        return Err(anyhow!(
            "CreateProcessW(AppContainer) failed with Win32 error {create_error}"
        ));
    }
    let process_handle = Handle(process.hProcess);
    let thread_handle = Handle(process.hThread);
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
    Ok(exit_code as i32)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn windows_argument_quoting_preserves_quotes_and_trailing_slashes() {
        assert_eq!(quote_windows_argument("plain"), "plain");
        assert_eq!(quote_windows_argument("two words"), "\"two words\"");
        assert_eq!(quote_windows_argument(r#"a\"b\\"#), r#"\"a\\\"b\\\\\""#);
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
