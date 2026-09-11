mod protocol;

use anyhow::{anyhow, Context, Result};
use protocol::{Request, TerminalRecord, MAX_REQUEST_BYTES, PROTOCOL_VERSION};
use std::io::{self, Read, Write};

#[cfg(windows)]
mod access_check;
#[cfg(windows)]
mod acl;
#[cfg(windows)]
mod acl_lock;
#[cfg(windows)]
mod global_acl_lock;
#[cfg(windows)]
mod host_prepare;
#[cfg(windows)]
mod identity;
#[cfg(windows)]
mod journal;
#[cfg(windows)]
mod process;
#[cfg(windows)]
mod windows;
#[cfg(windows)]
mod winutil;

fn read_request() -> Result<Request> {
    let mut input = io::stdin().lock();
    let mut size_bytes = [0_u8; 4];
    input
        .read_exact(&mut size_bytes)
        .context("request is missing its 4-byte length prefix")?;
    let size = u32::from_le_bytes(size_bytes) as usize;
    if size == 0 || size > MAX_REQUEST_BYTES {
        return Err(anyhow!(
            "request length {size} is outside the supported 1..={MAX_REQUEST_BYTES} range"
        ));
    }
    let mut body = vec![0_u8; size];
    input
        .read_exact(&mut body)
        .context("request frame ended before its declared length")?;
    let request: Request =
        serde_json::from_slice(&body).context("request is not valid protocol JSON")?;
    if request.protocol_version() != PROTOCOL_VERSION {
        return Err(anyhow!(
            "unsupported protocolVersion {}; expected {PROTOCOL_VERSION}",
            request.protocol_version()
        ));
    }
    Ok(request)
}

fn write_record(record: &TerminalRecord<'_>) -> Result<()> {
    let stderr = io::stderr();
    let mut writer = stderr.lock();
    serde_json::to_writer(&mut writer, record).context("could not serialize terminal record")?;
    writer.write_all(b"\n")?;
    writer.flush()?;
    Ok(())
}

#[cfg(windows)]
fn execute(request: Request) -> Result<i32> {
    windows::execute(request)
}

#[cfg(not(windows))]
fn execute(_request: Request) -> Result<i32> {
    Err(anyhow!("bello-windows-sandbox can run only on Windows"))
}

fn normalize_drive(value: &str) -> Result<String> {
    let bytes = value.as_bytes();
    if bytes.len() != 2 || !bytes[0].is_ascii_alphabetic() || bytes[1] != b':' {
        return Err(anyhow!("--drive accepts exactly one drive letter and colon, for example D:; paths are not accepted"));
    }
    Ok(format!("{}:", (bytes[0] as char).to_ascii_uppercase()))
}

fn parse_host_arguments(
    mut arguments: impl Iterator<Item = std::ffi::OsString>,
) -> Result<(&'static str, Option<String>)> {
    let argument = arguments
        .next()
        .ok_or_else(|| anyhow!("missing fixed host operation"))?;
    let operation = match argument.to_str() {
        Some("host-status") => "status",
        Some("host-prepare") => "prepare",
        Some("host-remove") => "remove",
        _ => {
            return Err(anyhow!(
                "expected host-status, host-prepare, or host-remove"
            ))
        }
    };
    let drive = match arguments.next() {
        None => None,
        Some(option) if option == "--drive" => {
            let value = arguments
                .next()
                .ok_or_else(|| anyhow!("--drive requires one drive letter"))?;
            Some(normalize_drive(
                value
                    .to_str()
                    .ok_or_else(|| anyhow!("--drive must be ASCII"))?,
            )?)
        }
        _ => {
            return Err(anyhow!(
                "only an optional --drive D: selector is accepted; no paths or commands"
            ))
        }
    };
    if arguments.next().is_some() {
        return Err(anyhow!(
            "host preparation accepts only one optional drive selector"
        ));
    }
    Ok((operation, drive))
}

fn dispatch() -> Result<Option<i32>> {
    let arguments = std::env::args_os().skip(1).collect::<Vec<_>>();
    if arguments.is_empty() {
        return read_request().and_then(execute).map(Some);
    }
    let (operation, drive) = parse_host_arguments(arguments.into_iter())?;
    #[cfg(windows)]
    {
        // This branch never reads framed stdin, configuration, run/recovery
        // requests, or invokes a user-controlled command with elevated rights.
        let report = match drive.as_deref() {
            Some(drive) => host_prepare::execute_on_drive(operation, drive)?,
            None => host_prepare::execute(operation)?,
        };
        let stdout = io::stdout();
        let mut writer = stdout.lock();
        serde_json::to_writer(&mut writer, &report)?;
        writer.write_all(b"\n")?;
        writer.flush()?;
        Ok(None)
    }
    #[cfg(not(windows))]
    {
        let _ = (operation, drive);
        Err(anyhow!("Windows host preparation can run only on Windows"))
    }
}

fn real_main() -> i32 {
    std::panic::set_hook(Box::new(|_| {}));
    match std::panic::catch_unwind(dispatch) {
        Ok(Ok(None)) => 0,
        Ok(Ok(Some(exit_code))) => {
            if write_record(&TerminalRecord::Exit {
                protocol_version: PROTOCOL_VERSION,
                exit_code,
            })
            .is_ok()
            {
                0
            } else {
                3
            }
        }
        Ok(Err(error)) => {
            let message = format!("{error:#}");
            let _ = write_record(&TerminalRecord::Error {
                protocol_version: PROTOCOL_VERSION,
                code: "sandbox_backend_error",
                message: &message,
            });
            2
        }
        Err(_) => {
            let _ = write_record(&TerminalRecord::Error {
                protocol_version: PROTOCOL_VERSION,
                code: "sandbox_backend_panic",
                message: "the native sandbox helper aborted unexpectedly",
            });
            4
        }
    }
}

fn main() {
    std::process::exit(real_main());
}

#[cfg(test)]
mod host_argument_tests {
    use super::*;

    #[test]
    fn only_a_single_literal_drive_selector_is_accepted() {
        let parse = |args: &[&str]| parse_host_arguments(args.iter().map(std::ffi::OsString::from));
        assert_eq!(parse(&["host-status"]).unwrap(), ("status", None));
        assert_eq!(
            parse(&["host-prepare", "--drive", "d:"]).unwrap(),
            ("prepare", Some("D:".to_owned()))
        );
        for drive in [
            "D",
            "D:\\",
            "D:/",
            "D:\\folder",
            "D:folder",
            "\\\\server\\share",
            "*:",
            "Ｄ:",
            "D:;cmd",
            "D:\0",
            "D: ",
        ] {
            assert!(
                parse(&["host-prepare", "--drive", drive]).is_err(),
                "accepted {drive:?}"
            );
        }
        for args in [
            vec!["host-prepare", "--drive"],
            vec!["host-status", "D:"],
            vec!["host-remove", "--drive", "D:", "--drive", "C:"],
            vec!["host-prepare", "--drive=D:"],
            vec!["run", "--drive", "D:"],
        ] {
            assert!(parse(&args).is_err(), "accepted {args:?}");
        }
    }
}
