// 确认残留 python 是不是本项目 talk-script-agent 的引擎进程（看命令行中的端口/脚本名）
"use strict";
const { execFileSync } = require("child_process");

function runPs(script) {
  const enc = Buffer.from("﻿" + script, "utf16le").toString("base64");
  return execFileSync("powershell", ["-NoProfile", "-NonInteractive", "-EncodedCommand", enc], {
    encoding: "utf8", timeout: 60000, maxBuffer: 8 * 1024 * 1024,
    stdio: ["ignore", "pipe", "ignore"],
  });
}

const script = [
  "$ProgressPreference = 'SilentlyContinue'",
  "$ErrorActionPreference = 'SilentlyContinue'",
  "$all = Get-CimInstance Win32_Process -Filter \"Name = 'python.exe' or Name = 'pythonw.exe'\"",
  "foreach ($x in $all) {",
  "  $id = $x.ProcessId; $cmd = $x.CommandLine",
  "  $isProj = 'no'",
  "  if ($cmd -and ($cmd -match 'talk-script-agent' -or $cmd -match 'app.main' -or $cmd -match 'uvicorn' -or $cmd -match '--port')) { $isProj = 'YES' }",
  "  Write-Output ('--- pid=' + $id + '  project=' + $isProj)",
  "  Write-Output ('    parent=' + $x.ParentProcessId + '  create=' + $x.CreationDate)",
  "  Write-Output ('    cmd=' + ($cmd -replace '\\s+',' '))",
  "}",
  "Write-Output '=== 各进程监听端口 ==='",
  "Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalAddress -eq '127.0.0.1' } | Sort-Object LocalPort | ForEach-Object { Write-Output ($_.LocalPort.ToString() + ' -> pid ' + $_.OwningProcess) }",
  "exit 0",
].join("\r\n");

try {
  console.log(runPs(script).trim());
} catch (e) {
  console.log("查询失败:", e.message);
  console.log(e.stdout || "");
}
