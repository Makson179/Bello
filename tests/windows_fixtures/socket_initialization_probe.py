"""Failure-only Windows diagnostic. Never bind, connect, send, or mutate device ACLs.

Copied beside the staged interpreter. Raw host descriptors stay in that exact
readable fixture directory and are never included in diagnostic output.
"""
from __future__ import annotations

import base64
import ctypes as c
import importlib
import json
from pathlib import Path
import socket
import sys
import uuid

_AFD_PATHS = (r"\Device\Afd", r"\Device\Afd\Endpoint")
_READ_CONTROL = 0x20000


class UnicodeString(c.Structure):
    _fields_ = [("Length", c.c_ushort), ("MaximumLength", c.c_ushort), ("Buffer", c.c_void_p)]


class ObjectAttributes(c.Structure):
    _fields_ = [("Length", c.c_uint32), ("RootDirectory", c.c_void_p), ("ObjectName", c.c_void_p),
                ("Attributes", c.c_uint32), ("SecurityDescriptor", c.c_void_p), ("SecurityQualityOfService", c.c_void_p)]


class IoStatus(c.Structure):
    _fields_ = [("Status", c.c_void_p), ("Information", c.c_size_t)]


class GenericMapping(c.Structure):
    _fields_ = [("Read", c.c_uint32), ("Write", c.c_uint32), ("Execute", c.c_uint32), ("All", c.c_uint32)]


def function(library, name, restype, argtypes):
    value = getattr(library, name)
    value.restype, value.argtypes = restype, argtypes
    return value


def nt_hex(status):
    return f"0x{status & 0xffffffff:08x}"


def afd_descriptor(path, nt, close):
    text = c.create_unicode_buffer(path)
    name = UnicodeString(len(path) * 2, (len(path) + 1) * 2, c.cast(text, c.c_void_p))
    attributes = ObjectAttributes(c.sizeof(ObjectAttributes), None, c.cast(c.pointer(name), c.c_void_p),
                                  0x1040, None, None)  # CASE_INSENSITIVE | DONT_REPARSE
    handle, io = c.c_void_p(), IoStatus()
    status = nt.NtOpenFile(c.byref(handle), _READ_CONTROL, c.byref(attributes), c.byref(io), 7, 0)
    report = {"openNtStatus": nt_hex(status), "requestedAccess": "READ_CONTROL", "descriptorAvailable": False}
    if status < 0:
        report["inconclusive"] = ("INVALID_PARAMETER: endpoint open without EA does not test socket authorization"
                                  if status & 0xffffffff == 0xc000000d else
                                  "This READ_CONTROL device open is not the socket endpoint-create request")
        return report, None
    try:
        needed = c.c_uint32()
        status = nt.NtQuerySecurityObject(handle, 0x17, None, 0, c.byref(needed))  # OWNER | GROUP | DACL | LABEL
        if status & 0xffffffff != 0xc0000023 or not 0 < needed.value <= 65536:
            report.update(queryNtStatus=nt_hex(status), inconclusive="No bounded owner/group/DACL/label descriptor; no privilege escalation")
            return report, None
        buffer = c.create_string_buffer(needed.value)
        capacity = needed.value
        status = nt.NtQuerySecurityObject(handle, 0x17, buffer, capacity, c.byref(needed))
        report["queryNtStatus"] = nt_hex(status)
        if status < 0 or not 0 < needed.value <= capacity:
            report["inconclusive"] = "Descriptor query failed; this is not an access-denied verdict"
            return report, None
        report["descriptorAvailable"] = True
        return report, buffer.raw[:needed.value]
    finally:
        ok = close(handle)
        report["closeOk"] = bool(ok)
        if not ok:
            report["closeWinError"] = c.get_last_error()


def duplicate_own_token(kernel, security):
    token, duplicate = c.c_void_p(), c.c_void_p()
    if not security.OpenProcessToken(kernel.GetCurrentProcess(), 0x000a, c.byref(token)):
        return None, {"inconclusive": "OpenProcessToken failed", "winerror": c.get_last_error()}
    report = {}
    try:
        is_container, needed = c.c_uint32(), c.c_uint32()
        if security.GetTokenInformation(token, 29, c.byref(is_container), 4, c.byref(needed)):
            report["isAppContainer"] = bool(is_container.value)
        else:
            report["tokenQueryWinError"] = c.get_last_error()
        # Identification-level impersonation token, never impersonated by this process.
        if not security.DuplicateTokenEx(token, 0x0008, None, 1, 2, c.byref(duplicate)):
            report.update(inconclusive="DuplicateTokenEx failed", winerror=c.get_last_error())
            return None, report
        return duplicate, report
    finally:
        kernel.CloseHandle(token)


def check_descriptor(raw, token, security):
    if token is None:
        return {"inconclusive": "No duplicated current-process token"}
    descriptor = c.create_string_buffer(raw)
    # Explicit diagnostic mapping, not an assertion about AFD endpoint-create masks.
    mapping = GenericMapping(0x120089, 0x120116, 0x1200a0, 0x1f01ff)
    size = c.c_uint32(1024)
    for _ in range(3):
        if not 0 < size.value <= 65536:
            break
        capacity = size.value
        privileges = c.create_string_buffer(capacity)
        granted, allowed = c.c_uint32(), c.c_int()
        ok = security.AccessCheck(descriptor, token, 0x02000000, c.byref(mapping), privileges,
                                  c.byref(size), c.byref(granted), c.byref(allowed))
        error = 0 if ok else c.get_last_error()
        if ok:
            return {"apiOk": True, "accessStatus": bool(allowed.value), "grantedMask": hex(granted.value),
                    "mapping": "FILE_GENERIC/MAXIMUM_ALLOWED", "socketAuthorization": "not established by this check"}
        if error != 122 or size.value <= capacity:
            return {"apiOk": False, "winerror": error, "inconclusive": "AccessCheck API failure is not an access verdict"}
    return {"inconclusive": "AccessCheck privilege buffer exceeded bounded retries"}


def socket_probe():
    report = {}
    try:
        importlib.import_module("_overlapped")
        report["import_overlapped"] = {"ok": True}
    except Exception as exc:
        report["import_overlapped"] = {"ok": False, "type": type(exc).__name__,
                                        "winerror": getattr(exc, "winerror", None), "error": str(exc)}
    ws2 = c.WinDLL("ws2_32")
    function(ws2, "socket", c.c_size_t, [c.c_int, c.c_int, c.c_int])
    function(ws2, "WSAGetLastError", c.c_int, [])
    function(ws2, "closesocket", c.c_int, [c.c_size_t])
    function(ws2, "WSAIoctl", c.c_int, [c.c_size_t, c.c_uint32, c.c_void_p, c.c_uint32,
        c.c_void_p, c.c_uint32, c.POINTER(c.c_uint32), c.c_void_p, c.c_void_p])
    handle = ws2.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
    invalid = handle == c.c_size_t(-1).value
    error = ws2.WSAGetLastError() if invalid else 0
    report["socket_AF_INET_STREAM_TCP"] = {"ok": not invalid, "winerror": error}
    if not invalid:
        try:
            guid = c.create_string_buffer(uuid.UUID("b5367df1-cbac-11cf-95ca-00805f48a192").bytes_le)
            pointer, returned = c.c_void_p(), c.c_uint32()
            result = ws2.WSAIoctl(handle, 0xc8000006, guid, 16, c.byref(pointer),
                                  c.sizeof(pointer), c.byref(returned), None, None)
            error = ws2.WSAGetLastError() if result == -1 else 0
            report["WSAIoctl_AcceptEx"] = {"ok": result == 0, "winerror": error,
                                          "returnedBytes": returned.value, "nonNullPointer": bool(pointer.value)}
        finally:
            result = ws2.closesocket(handle)
            report["closesocket"] = {"ok": result == 0, "winerror": ws2.WSAGetLastError() if result == -1 else 0}
    return report


def main():
    mode, descriptor_file = sys.argv[1:]
    if mode not in {"host", "sandbox"}:
        raise ValueError("unknown diagnostic mode")
    nt, kernel, security = (c.WinDLL(name, use_last_error=True) for name in ("ntdll", "kernel32", "advapi32"))
    function(nt, "NtOpenFile", c.c_int32, [c.POINTER(c.c_void_p), c.c_uint32,
        c.POINTER(ObjectAttributes), c.POINTER(IoStatus), c.c_uint32, c.c_uint32])
    function(nt, "NtQuerySecurityObject", c.c_int32, [c.c_void_p, c.c_uint32, c.c_void_p, c.c_uint32, c.POINTER(c.c_uint32)])
    function(kernel, "CloseHandle", c.c_int, [c.c_void_p])
    function(kernel, "GetCurrentProcess", c.c_void_p, [])
    function(security, "OpenProcessToken", c.c_int, [c.c_void_p, c.c_uint32, c.POINTER(c.c_void_p)])
    function(security, "GetTokenInformation", c.c_int, [c.c_void_p, c.c_int, c.c_void_p, c.c_uint32, c.POINTER(c.c_uint32)])
    function(security, "DuplicateTokenEx", c.c_int, [c.c_void_p, c.c_uint32, c.c_void_p, c.c_int, c.c_int, c.POINTER(c.c_void_p)])
    function(security, "AccessCheck", c.c_int, [c.c_void_p, c.c_void_p, c.c_uint32,
        c.POINTER(GenericMapping), c.c_void_p, c.POINTER(c.c_uint32), c.POINTER(c.c_uint32), c.POINTER(c.c_int)])
    descriptors = {} if mode == "host" else json.loads(Path(descriptor_file).read_text(encoding="ascii"))
    token, identity = duplicate_own_token(kernel, security)
    report = {"mode": mode, "token": identity, "afd": {}}
    try:
        try:
            report["socket"] = socket_probe()
        except Exception as exc:
            report["socket"] = {"inconclusive": f"Socket diagnostic failed: {type(exc).__name__}: {exc}"}
        for path in _AFD_PATHS:
            result, raw = afd_descriptor(path, nt, kernel.CloseHandle)
            if mode == "host" and raw is not None:
                descriptors[path] = base64.b64encode(raw).decode("ascii")
            if path in descriptors:
                host_raw = base64.b64decode(descriptors[path], validate=True)
                if not 0 < len(host_raw) <= 65536:
                    raise ValueError("invalid host descriptor length")
                result["accessCheckHostSD"] = check_descriptor(host_raw, token, security)
                if raw is not None:
                    result["descriptorMatchesHost"] = raw == host_raw
            else:
                result["accessCheckHostSD"] = {"inconclusive": "Host could not query this device descriptor"}
            report["afd"][path] = result
        if mode == "host":
            Path(descriptor_file).write_text(json.dumps(descriptors), encoding="ascii")
    finally:
        if token is not None:
            report["tokenCloseOk"] = bool(kernel.CloseHandle(token))
    print(json.dumps(report, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
