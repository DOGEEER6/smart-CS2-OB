# 死机取证脚本（只读，不改任何系统设置）
#
# 用法：在**出问题的那台电脑**上，右键 PowerShell → 以管理员身份运行，然后：
#     cd <本仓库目录>
#     powershell -ExecutionPolicy Bypass -File tools\diagnose_freeze.ps1
#
# 它会收集：机器信息 / 内存与页面文件 / OBS 画布与回放插件参数（用来算插件要多少内存）/
#           系统日志里的死机相关事件（Kernel-Power 41、BugCheck、WHEA、显卡 TDR、
#           磁盘超时、内存耗尽、应用挂起）/ 崩溃转储 / 当前进程内存快照。
# 结果写成一个 UTF-8 文本文件，路径会打印在最后 —— 把它发给我就行。
#
# ⚠️ 全程只读：不修改注册表、不改服务、不动任何文件（只在桌面写一个报告 txt）。

param(
    [int]$Days = 30,
    [string]$OutFile = ""
)

$ErrorActionPreference = "Continue"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
if (-not $OutFile) { $OutFile = Join-Path ([Environment]::GetFolderPath("Desktop")) "freeze-report-$stamp.txt" }
$sb = New-Object System.Text.StringBuilder

function Say($t) { [void]$sb.AppendLine($t); Write-Host $t }
function Head($t) { Say ""; Say ("=" * 72); Say "  $t"; Say ("=" * 72) }

Head "死机取证报告  $stamp"
Say "报告只包含诊断信息，不含密码；生成过程全程只读。"

# ---------------------------------------------------------------- 机器信息
Head "1. 机器与系统"
try {
    $os = Get-CimInstance Win32_OperatingSystem
    Say ("计算机名    : " + $env:COMPUTERNAME)
    Say ("系统        : " + $os.Caption + "  Build " + $os.BuildNumber)
    Say ("总内存      : " + [math]::Round($os.TotalVisibleMemorySize / 1MB, 2) + " GB")
    Say ("当前可用    : " + [math]::Round($os.FreePhysicalMemory / 1MB, 2) + " GB")
    Say ("提交/上限   : " + [math]::Round(($os.TotalVirtualMemorySize - $os.FreeVirtualMemory) / 1MB, 2) +
        " / " + [math]::Round($os.TotalVirtualMemorySize / 1MB, 2) + " GB")
    Say ("上次启动    : " + $os.LastBootUpTime)
    Say ("CPU         : " + (Get-CimInstance Win32_Processor | Select-Object -First 1 -Expand Name))
} catch { Say ("读取系统信息失败: " + $_) }

try {
    $pf = Get-CimInstance Win32_PageFileUsage
    if ($pf) { $pf | ForEach-Object { Say ("页面文件    : " + $_.Name + "  当前 " + $_.CurrentUsage + " MB / 峰值 " + $_.PeakUsage + " MB / 上限 " + $_.AllocatedBaseSize + " MB") } }
    else { Say "页面文件    : （没有页面文件 / 由系统托管）" }
    Say ("页面文件自动管理: " + (Get-CimInstance Win32_ComputerSystem).AutomaticManagedPagefile)
} catch { Say ("读取页面文件信息失败: " + $_) }

try {
    Get-CimInstance Win32_VideoController | ForEach-Object {
        Say ("显卡        : " + $_.Name + "   驱动 " + $_.DriverVersion + " (" + $_.DriverDate + ")")
    }
} catch {}

try {
    $pp = powercfg /getactivescheme 2>$null
    Say ("电源计划    : " + ($pp -join " "))
} catch {}

# ---------------------------------------------------------------- 内存压力
Head "2. 当前内存占用 Top 15（私有内存）"
try {
    Get-Process | Sort-Object -Property PrivateMemorySize64 -Descending | Select-Object -First 15 |
        ForEach-Object {
            Say ("  {0,-28} 私有 {1,8:N0} MB   工作集 {2,8:N0} MB" -f $_.ProcessName,
                 ($_.PrivateMemorySize64 / 1MB), ($_.WorkingSet64 / 1MB))
        }
} catch { Say ("读取进程内存失败: " + $_) }

Say ""
Say "重点看这几个进程（有就说明当时在跑）："
foreach ($n in @("obs64", "cs2", "开始导播", "python", "Astra", "verge-mihomo")) {
    $p = Get-Process -Name $n -ErrorAction SilentlyContinue
    if ($p) {
        $p | ForEach-Object { Say ("  {0,-12} PID {1,-8} 私有 {2,8:N0} MB" -f $_.ProcessName, $_.Id, ($_.PrivateMemorySize64 / 1MB)) }
    } else { Say ("  {0,-12} 未运行" -f $n) }
}

# ---------------------------------------------------------------- OBS 配置
Head "3. OBS 画布与回放插件参数（用来算插件要占多少内存）"
$obsBasic = Join-Path $env:APPDATA "obs-studio\basic\profiles"
$foundCanvas = $false
if (Test-Path $obsBasic) {
    Get-ChildItem $obsBasic -Directory | ForEach-Object {
        $ini = Join-Path $_.FullName "basic.ini"
        if (Test-Path $ini) {
            $txt = Get-Content $ini -Raw
            $bw = [regex]::Match($txt, "BaseCX=(\d+)").Groups[1].Value
            $bh = [regex]::Match($txt, "BaseCY=(\d+)").Groups[1].Value
            $ow = [regex]::Match($txt, "OutputCX=(\d+)").Groups[1].Value
            $oh = [regex]::Match($txt, "OutputCY=(\d+)").Groups[1].Value
            $fps = [regex]::Match($txt, "FPSCommon=(\d+)").Groups[1].Value
            if ($bw -and $bh) {
                $foundCanvas = $true
                Say ("配置「" + $_.Name + "」: 画布 " + $bw + "x" + $bh + "  输出 " + $ow + "x" + $oh + "  FPS " + $fps)
                $f = if ($fps) { [double]$fps } else { 60 }
                foreach ($sec in @(10, 5)) {
                    $oneGB = ([double]$bw * [double]$bh * 4 * $f * $sec) / 1e9
                    Say ("    → 回放插件缓冲 " + $sec + " 秒：一份 ≈ " + [math]::Round($oneGB, 2) +
                         " GB，两份（缓冲+已取出）≈ " + [math]::Round($oneGB * 2, 2) + " GB")
                }
            }
        }
    }
}
if (-not $foundCanvas) { Say "  （没读到 OBS 配置；OBS 里的画布分辨率请手动告诉我）" }

$scenes = Join-Path $env:APPDATA "obs-studio\basic\scenes"
if (Test-Path $scenes) {
    Say ""
    Say "  场景集合里的 replay_source / Replay Source 实例（每个都自己攒一份缓冲）："
    Get-ChildItem $scenes -Filter *.json | ForEach-Object {
        $t = Get-Content $_.FullName -Raw
        $n = ([regex]::Matches($t, '"replay_source"')).Count
        if ($n -gt 0) { Say ("    " + $_.Name + " : " + $n + " 处 replay_source") }
    }
    Say "  （另外提醒：插件属性里的 Maximum replays 每多 1 就再多一整份内存）"
}

# ---------------------------------------------------------------- 系统日志
Head "4. 系统日志里的死机相关事件（近 $Days 天）"
$since = (Get-Date).AddDays(-$Days)
$patterns = @(
    @{ p = "Microsoft-Windows-Kernel-Power"; i = 41;   d = "★ 未正常关机/硬挂（看 BugcheckCode：0 = 没有蓝屏，是硬挂或断电）" },
    @{ p = "Microsoft-Windows-Kernel-Power"; i = 6008; d = "" },
    @{ p = "EventLog";                       i = 6008; d = "★ 上一次关机是意外的" },
    @{ p = "Microsoft-Windows-WHEA-Logger";  i = $null; d = "★ 硬件报错（CPU/内存/PCIe）" },
    @{ p = "Microsoft-Windows-Kernel-Power"; i = 42;   d = "进入睡眠" },
    @{ p = "Microsoft-Windows-Kernel-Boot";  i = 29;   d = "快速启动失败" },
    @{ p = "Display";                        i = 4101; d = "显卡驱动超时复位(TDR)" },
    @{ p = "Display";                        i = 4107; d = "" },
    @{ p = "disk";                           i = 11;   d = "磁盘控制器错误" },
    @{ p = "disk";                           i = 129;  d = "磁盘复位（超时）" },
    @{ p = "disk";                           i = 153;  d = "磁盘 I/O 重试" },
    @{ p = "Microsoft-Windows-Resource-Exhaustion-Detector"; i = $null; d = "★ 内存耗尽" },
    @{ p = "Application Hang";               i = 1002; d = "★ 应用卡死" },
    @{ p = "Application Error";              i = 1000; d = "应用崩溃" },
    @{ p = "nvlddmkm";                       i = $null; d = "NVIDIA 驱动报错" },
    @{ p = "Microsoft-Windows-Kernel-Processor-Power"; i = 55; d = "CPU 电源/降频" },
    @{ p = "Microsoft-Windows-Kernel-Thermal"; i = $null; d = "过热" }
)

foreach ($pat in $patterns) {
    try {
        $f = @{ LogName = "System"; StartTime = $since }
        if ($pat.p) { $f["ProviderName"] = $pat.p }
        if ($pat.i) { $f["Id"] = $pat.i }
        $ev = Get-WinEvent -FilterHashtable $f -MaxEvents 12 -ErrorAction SilentlyContinue
        if ($ev) {
            Say ""
            Say ("--- " + $pat.p + " " + $pat.i + "  " + $pat.d + "   （共取到 " + $ev.Count + " 条）")
            foreach ($e in $ev) {
                $extra = ""
                try {
                    $xml = [xml]$e.ToXml()
                    $bc = ($xml.Event.EventData.Data | Where-Object { $_.Name -eq "BugcheckCode" })."#text"
                    if ($bc) { $extra = "  BugcheckCode=$bc" }
                } catch {}
                $firstLine = ($e.Message -split "`r?`n")[0]
                Say ("    " + $e.TimeCreated.ToString("MM-dd HH:mm:ss") + "  Id=" + $e.Id + $extra + "  " + $firstLine)
            }
        }
    } catch {}
}
# Application 日志里的"应用卡死/崩溃"（上面按 System 取不到，单独走 Application）
foreach ($pat in @(@{p="Application Hang"; i=1002}, @{p="Application Error"; i=1000})) {
    try {
        $ev = Get-WinEvent -FilterHashtable @{ LogName = "Application"; ProviderName = $pat.p; StartTime = $since } -MaxEvents 12 -ErrorAction SilentlyContinue
        if ($ev) {
            Say ""
            Say ("--- " + $pat.p + "（应用程序日志，共取到 " + $ev.Count + " 条）")
            foreach ($e in $ev) { Say ("    " + $e.TimeCreated.ToString("MM-dd HH:mm:ss") + "  " + (($e.Message -split "`r?`n")[0])) }
        }
    } catch {}
}

# ---------------------------------------------------------------- 转储
Head "5. 崩溃转储（有转储 = 蓝屏过；没有 = 硬挂/断电）"
$md = "C:\Windows\Minidump"
if (Test-Path $md) {
    $f = Get-ChildItem $md -Filter *.dmp -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 10
    if ($f) { $f | ForEach-Object { Say ("  " + $_.LastWriteTime.ToString("yyyy-MM-dd HH:mm") + "  " + $_.Name + "  " + [math]::Round($_.Length / 1MB, 1) + " MB") } }
    else { Say "  Minidump 目录是空的（近期没有蓝屏）" }
} else { Say "  没有 C:\Windows\Minidump 目录（说明基本没蓝屏过）" }
$mem = "C:\Windows\MEMORY.DMP"
if (Test-Path $mem) { Say ("  完整转储: " + [math]::Round((Get-Item $mem).Length / 1GB, 2) + " GB, " + (Get-Item $mem).LastWriteTime) }
else { Say "  没有 C:\Windows\MEMORY.DMP" }
Say ""
Say "  内存诊断结果（如果有跑过 Windows 内存诊断）："
try {
    $mdres = Get-WinEvent -FilterHashtable @{ LogName = "System"; ProviderName = "Microsoft-Windows-MemoryDiagnostics-Results" } -MaxEvents 5 -ErrorAction SilentlyContinue
    if ($mdres) { $mdres | ForEach-Object { Say ("    " + $_.TimeCreated + "  " + ($_.Message -split "`r?`n")[0]) } }
    else { Say "    （没跑过，或没结果）" }
} catch {}

# ---------------------------------------------------------------- 第三方输入层
Head "6. 可能与键盘钩子互相影响的第三方输入层 / 过滤器驱动"
try {
    Say "-- 可疑服务（输入/键盘/鼠标/虚拟设备/远程控制类）--"
    Get-Service | Where-Object { $_.Name -match "input|key|mouse|hid|virtual|parsec|logi|razer|corsair|steel|hyperx|interception|vmulti|vigem|vgamepad|sunshine|toDesk|AnyDesk|GameViewer" } |
        Select-Object Status, StartType, Name, DisplayName | ForEach-Object {
            Say ("  {0,-9} {1,-11} {2,-22} {3}" -f $_.Status, $_.StartType, $_.Name, $_.DisplayName)
        }
    Say ""
    Say "-- 内核过滤器驱动（fltmc）--"
    (fltmc filters 2>$null) | ForEach-Object { Say ("  " + $_) }
    Say ""
    Say "-- 可疑的第三方内核驱动（driverquery，只列非微软签名可疑名）--"
    (driverquery /v /fo csv 2>$null | ConvertFrom-Csv |
        Where-Object { $_.'Display Name' -match "input|hid|kbd|mou|virtual|parsec|interception|vmulti" }) |
        Select-Object -First 20 | ForEach-Object { Say ("  " + $_.'Module Name' + "  |  " + $_.'Display Name' + "  |  " + $_.'Path') }
} catch { Say ("读取驱动列表失败: " + $_) }

# ---------------------------------------------------------------- 结论提示
Head "7. 这份报告怎么读（给我看的时候重点看这几条）"
Say "  · 第 4 节里 Kernel-Power 41 的 BugcheckCode：0 = 没有蓝屏（硬挂或断电）；非 0 = 蓝屏，把那行发我。"
Say "  · 有没有 WHEA-Logger（有 = 硬件层报错，跟本软件无关）。"
Say "  · 有没有 Display 4101（显卡驱动复位）——有就偏向显卡/驱动。"
Say "  · 有没有 Resource-Exhaustion-Detector（内存耗尽）——有就偏向内存被吃光（回放插件嫌疑大）。"
Say "  · 第 3 节算出来的插件内存，和机器的总内存对一下："
Say "      两份 > 可用内存 → 换页风暴，几乎一定会卡死。"
Say "  · 第 6 节里如果有别的键盘钩子/键盘过滤驱动（比如 inputx64、interception），"
Say "      它们和我们引擎的全局键盘钩子可能互相影响 —— 这一条要单独做对照实验。"

$txt = $sb.ToString()
[System.IO.File]::WriteAllText($OutFile, $txt, (New-Object System.Text.UTF8Encoding($false)))
Write-Host ""
Write-Host "=============================================================="
Write-Host " 报告已生成：$OutFile"
Write-Host " 把这个文件发给我即可（纯文本，不含密码）。"
Write-Host "=============================================================="
